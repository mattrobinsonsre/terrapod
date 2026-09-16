// Package module_autodiscovery_rule_repositories implements the
// terrapod_module_autodiscovery_rule_repositories data source (#1620): the
// repositories a module autodiscovery rule looks at, each with the state its
// polls keep. It is read-only and separate from the rule resource on purpose:
// the state changes with every poll, so embedding it in the rule would churn
// Terraform state.
//
// API Contract (Terrapod API <-> Terraform Provider), via go-terrapod:
//
//	GET /api/terrapod/v1/module-autodiscovery-rules/{id}/repositories[?filter[status]=…]
//	JSON:API type: "module-autodiscovery-rule-repositories", ID "modrepo-<uuid>"
package module_autodiscovery_rule_repositories

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var _ datasource.DataSource = &repositoriesDataSource{}

type repositoriesDataSource struct {
	tc *terrapod.Client
}

type repositoriesModel struct {
	RuleID       types.String      `tfsdk:"rule_id"`
	Status       types.String      `tfsdk:"status"`
	Repositories []repositoryEntry `tfsdk:"repositories"`
}

type repositoryEntry struct {
	ID                 types.String       `tfsdk:"id"`
	Repository         types.String       `tfsdk:"repository"`
	RepoURL            types.String       `tfsdk:"repo_url"`
	VCSRepoID          types.String       `tfsdk:"vcs_repo_id"`
	DefaultBranch      types.String       `tfsdk:"default_branch"`
	Status             types.String       `tfsdk:"status"`
	Origin             types.String       `tfsdk:"origin"`
	CandidatePaths     types.List         `tfsdk:"candidate_subdirectories"`
	Candidates         []candidateEntry   `tfsdk:"candidates"`
	SeenSubdirectories types.List         `tfsdk:"seen_subdirectories"`
	LastSkips          []skipEntry        `tfsdk:"last_skips"`
	PreviousPaths      types.List         `tfsdk:"previous_paths"`
	PreviousLocations  []previousLocation `tfsdk:"previous_locations"`
	LastScannedSHA     types.String       `tfsdk:"last_scanned_sha"`
	LastCheckedAt      types.String       `tfsdk:"last_checked_at"`
	NextCheckAt        types.String       `tfsdk:"next_check_at"`
	LastError          types.String       `tfsdk:"last_error"`
	FailureCount       types.Int64        `tfsdk:"failure_count"`
	FirstSeenAt        types.String       `tfsdk:"first_seen_at"`
	RepoCreatedAt      types.String       `tfsdk:"repo_created_at"`
}

// candidateEntry is a module the last poll found, with the name and provider it
// would register under — the part someone deciding what to scan actually needs,
// and which candidate_subdirectories alone cannot answer.
type candidateEntry struct {
	Subdirectory types.String `tfsdk:"subdirectory"`
	Name         types.String `tfsdk:"name"`
	Provider     types.String `tfsdk:"provider"`
}

// skipEntry is why one candidate registered nothing on the last scan. A
// repository's stored skips carry the subdirectory and the reason only.
type skipEntry struct {
	Subdirectory types.String `tfsdk:"subdirectory"`
	Reason       types.String `tfsdk:"reason"`
}

// previousLocation is a path the repository had before a rename or transfer,
// with the URL it had then. previous_paths keeps the paths alone, so that
// attribute's type never changed.
type previousLocation struct {
	Path types.String `tfsdk:"path"`
	URL  types.String `tfsdk:"url"`
}

// NewDataSource returns the terrapod_module_autodiscovery_rule_repositories data source.
func NewDataSource() datasource.DataSource {
	return &repositoriesDataSource{}
}

func (d *repositoriesDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_module_autodiscovery_rule_repositories"
}

func (d *repositoriesDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "The repositories a Terrapod module autodiscovery rule looks at, each with its status and the " +
			"candidates its last poll found: one for a single-repository rule once it has been polled, one per " +
			"listed repository for an org, group or pattern rule. Platform admin only.",
		Attributes: map[string]schema.Attribute{
			"rule_id": schema.StringAttribute{
				Required:    true,
				Description: "The rule ID (modrule-<uuid>).",
			},
			"status": schema.StringAttribute{
				Optional: true,
				Description: "Only repositories in this status: active, archived, empty, no-branch, out-of-scope, " +
					"covered or error.",
			},
			"repositories": schema.ListNestedAttribute{
				Computed:    true,
				Description: "The rule's repositories, by path.",
				NestedObject: schema.NestedAttributeObject{
					Attributes: map[string]schema.Attribute{
						"id":             schema.StringAttribute{Computed: true, Description: "The repository state's ID (modrepo-<uuid>)."},
						"repository":     schema.StringAttribute{Computed: true, Description: "The path: owner/repo, or group/subgroup/project."},
						"repo_url":       schema.StringAttribute{Computed: true, Description: "The repository's URL."},
						"vcs_repo_id":    schema.StringAttribute{Computed: true, Description: "The provider's own ID for the repository, which survives a rename."},
						"default_branch": schema.StringAttribute{Computed: true, Description: "The repository's default branch, or empty when not known."},
						"status": schema.StringAttribute{
							Computed: true,
							Description: "active, archived (kept, not scanned), empty, no-branch, out-of-scope (left the " +
								"rule's target; its modules stay), covered (a single-repository rule names it) or error.",
						},
						"origin": schema.StringAttribute{
							Computed: true,
							Description: "baseline (existed when the rule took its baseline: nothing registers until it " +
								"is scanned) or new (created afterwards: its modules register automatically).",
						},
						"candidate_subdirectories": schema.ListAttribute{
							Computed:    true,
							ElementType: types.StringType,
							Description: "The candidate module directories the last poll found (\"\" is the repository root).",
						},
						"candidates": schema.ListNestedAttribute{
							Computed:    true,
							Description: "The candidate modules the last poll found, with the name and provider each would register under.",
							NestedObject: schema.NestedAttributeObject{
								Attributes: map[string]schema.Attribute{
									"subdirectory": schema.StringAttribute{Computed: true, Description: "The directory (\"\" is the repository root)."},
									"name":         schema.StringAttribute{Computed: true, Description: "The module name it would register under."},
									"provider":     schema.StringAttribute{Computed: true, Description: "The provider it would register under."},
								},
							},
						},
						"seen_subdirectories": schema.ListAttribute{
							Computed:    true,
							ElementType: types.StringType,
							Description: "Directories already seen here, so automatic registration does not pick them up again.",
						},
						"last_skips": schema.ListNestedAttribute{
							Computed:    true,
							Description: "Why the last scan registered nothing for a candidate — the answer to \"why did this repository register nothing\".",
							NestedObject: schema.NestedAttributeObject{
								Attributes: map[string]schema.Attribute{
									"subdirectory": schema.StringAttribute{Computed: true, Description: "The directory that was skipped."},
									"reason":       schema.StringAttribute{Computed: true, Description: "already-registered, name-taken, missing-provider, or another reason."},
								},
							},
						},
						"previous_paths": schema.ListAttribute{
							Computed:    true,
							ElementType: types.StringType,
							Description: "Paths the repository had before a rename or transfer, oldest first.",
						},
						"previous_locations": schema.ListNestedAttribute{
							Computed:    true,
							Description: "The same renames as previous_paths, each with the URL the repository had then.",
							NestedObject: schema.NestedAttributeObject{
								Attributes: map[string]schema.Attribute{
									"path": schema.StringAttribute{Computed: true, Description: "The path it had then."},
									"url":  schema.StringAttribute{Computed: true, Description: "The URL it had then."},
								},
							},
						},
						"last_scanned_sha": schema.StringAttribute{Computed: true, Description: "The branch head at the last scan."},
						"last_checked_at":  schema.StringAttribute{Computed: true, Description: "When the branch head was last looked up (RFC3339), or empty."},
						"next_check_at":    schema.StringAttribute{Computed: true, Description: "When the repository is next due to be looked at (RFC3339), or empty; failures back off."},
						"last_error":       schema.StringAttribute{Computed: true, Description: "Why the repository could not be read, or empty."},
						"failure_count":    schema.Int64Attribute{Computed: true, Description: "Consecutive failed reads; they back off."},
						"first_seen_at":    schema.StringAttribute{Computed: true, Description: "When the rule first listed the repository (RFC3339)."},
						"repo_created_at":  schema.StringAttribute{Computed: true, Description: "When the provider says the repository was created (RFC3339), or empty."},
					},
				},
			},
		},
	}
}

func (d *repositoriesDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	c, ok := req.ProviderData.(*client.Client)
	if !ok {
		resp.Diagnostics.AddError("Unexpected provider data type", fmt.Sprintf("Expected *client.Client, got %T", req.ProviderData))
		return
	}
	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: c.BaseURL, Token: c.Token})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	d.tc = tc
}

func (d *repositoriesDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config repositoriesModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}
	status := ""
	if !config.Status.IsNull() && !config.Status.IsUnknown() {
		status = config.Status.ValueString()
	}
	repos, err := d.tc.ListAllModuleAutodiscoveryRuleRepositories(ctx, config.RuleID.ValueString(), status)
	if err != nil {
		resp.Diagnostics.AddError("Failed to list the rule's repositories", err.Error())
		return
	}
	entries, diags := entriesFrom(ctx, repos)
	resp.Diagnostics.Append(diags...)
	if resp.Diagnostics.HasError() {
		return
	}
	config.Repositories = entries
	resp.Diagnostics.Append(resp.State.Set(ctx, &config)...)
}

// entriesFrom maps the SDK's repositories onto the schema.
func entriesFrom(ctx context.Context, repos []terrapod.ModuleAutodiscoveryRepository) ([]repositoryEntry, diag.Diagnostics) {
	var diags diag.Diagnostics
	out := make([]repositoryEntry, 0, len(repos))
	for _, r := range repos {
		subs := make([]string, 0, len(r.Candidates))
		for _, c := range r.Candidates {
			subs = append(subs, c.Subdirectory)
		}
		previous := make([]string, 0, len(r.PreviousPaths))
		for _, p := range r.PreviousPaths {
			previous = append(previous, p.Path)
		}
		candidates := make([]candidateEntry, 0, len(r.Candidates))
		for _, c := range r.Candidates {
			candidates = append(candidates, candidateEntry{
				Subdirectory: types.StringValue(c.Subdirectory),
				Name:         types.StringValue(c.Name),
				Provider:     types.StringValue(c.Provider),
			})
		}
		skips := make([]skipEntry, 0, len(r.LastSkips))
		for _, s := range r.LastSkips {
			skips = append(skips, skipEntry{
				Subdirectory: types.StringValue(s.Subdirectory),
				Reason:       types.StringValue(s.Reason),
			})
		}
		locations := make([]previousLocation, 0, len(r.PreviousPaths))
		for _, p := range r.PreviousPaths {
			locations = append(locations, previousLocation{
				Path: types.StringValue(p.Path),
				URL:  types.StringValue(p.URL),
			})
		}
		subList, d := types.ListValueFrom(ctx, types.StringType, subs)
		diags.Append(d...)
		prevList, d := types.ListValueFrom(ctx, types.StringType, previous)
		diags.Append(d...)
		seenList, d := types.ListValueFrom(ctx, types.StringType, r.SeenSubdirectories)
		diags.Append(d...)
		out = append(out, repositoryEntry{
			ID:                 types.StringValue(r.ID),
			Repository:         types.StringValue(r.Repository),
			RepoURL:            types.StringValue(r.RepoURL),
			VCSRepoID:          types.StringValue(r.VCSRepoID),
			DefaultBranch:      types.StringValue(r.DefaultBranch),
			Status:             types.StringValue(r.Status),
			Origin:             types.StringValue(r.Origin),
			CandidatePaths:     subList,
			Candidates:         candidates,
			SeenSubdirectories: seenList,
			LastSkips:          skips,
			PreviousPaths:      prevList,
			PreviousLocations:  locations,
			LastScannedSHA:     types.StringValue(r.LastScannedSHA),
			LastCheckedAt:      types.StringValue(r.LastCheckedAt),
			NextCheckAt:        types.StringValue(r.NextCheckAt),
			LastError:          types.StringValue(r.LastError),
			FailureCount:       types.Int64Value(int64(r.FailureCount)),
			FirstSeenAt:        types.StringValue(r.FirstSeenAt),
			RepoCreatedAt:      types.StringValue(r.RepoCreatedAt),
		})
	}
	return out, diags
}
