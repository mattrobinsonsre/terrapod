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
	ID             types.String `tfsdk:"id"`
	Repository     types.String `tfsdk:"repository"`
	RepoURL        types.String `tfsdk:"repo_url"`
	DefaultBranch  types.String `tfsdk:"default_branch"`
	Status         types.String `tfsdk:"status"`
	Origin         types.String `tfsdk:"origin"`
	CandidatePaths types.List   `tfsdk:"candidate_subdirectories"`
	PreviousPaths  types.List   `tfsdk:"previous_paths"`
	LastScannedSHA types.String `tfsdk:"last_scanned_sha"`
	LastCheckedAt  types.String `tfsdk:"last_checked_at"`
	LastError      types.String `tfsdk:"last_error"`
	FailureCount   types.Int64  `tfsdk:"failure_count"`
	FirstSeenAt    types.String `tfsdk:"first_seen_at"`
	RepoCreatedAt  types.String `tfsdk:"repo_created_at"`
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
						"previous_paths": schema.ListAttribute{
							Computed:    true,
							ElementType: types.StringType,
							Description: "Paths the repository had before a rename or transfer, oldest first.",
						},
						"last_scanned_sha": schema.StringAttribute{Computed: true, Description: "The branch head at the last scan."},
						"last_checked_at":  schema.StringAttribute{Computed: true, Description: "When the branch head was last looked up (RFC3339), or empty."},
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
		subList, d := types.ListValueFrom(ctx, types.StringType, subs)
		diags.Append(d...)
		prevList, d := types.ListValueFrom(ctx, types.StringType, previous)
		diags.Append(d...)
		out = append(out, repositoryEntry{
			ID:             types.StringValue(r.ID),
			Repository:     types.StringValue(r.Repository),
			RepoURL:        types.StringValue(r.RepoURL),
			DefaultBranch:  types.StringValue(r.DefaultBranch),
			Status:         types.StringValue(r.Status),
			Origin:         types.StringValue(r.Origin),
			CandidatePaths: subList,
			PreviousPaths:  prevList,
			LastScannedSHA: types.StringValue(r.LastScannedSHA),
			LastCheckedAt:  types.StringValue(r.LastCheckedAt),
			LastError:      types.StringValue(r.LastError),
			FailureCount:   types.Int64Value(int64(r.FailureCount)),
			FirstSeenAt:    types.StringValue(r.FirstSeenAt),
			RepoCreatedAt:  types.StringValue(r.RepoCreatedAt),
		})
	}
	return out, diags
}
