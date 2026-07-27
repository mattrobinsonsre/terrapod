// Package architecture_critique implements the terrapod_architecture_critique
// data source (#1036 Part 2).
//
// API Contract: GET /api/terrapod/v1/workspaces/{id}/architecture-critique
// Reports the AI architecture critic's assessment of a workspace's CURRENT
// deployed system, inferred from its latest Terraform state and critiqued
// across reliability/security/cost/operations/scalability. Every finding is
// grounded in deterministic data (security ← the Checkov/Trivy scanner, with
// the rule id in grounded_in; cost ← the cost engine; the rest ← state + the
// resource graph) and anchored to a resource address. Useful in config for
// audit reporting or feeding the overall risk_level into a policy/check.
//
// Requires the optional ai_architecture feature to be enabled and the
// workspace to have state with a generated critique; otherwise the read
// returns a not-found error.
package architecture_critique

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var _ datasource.DataSource = &architectureCritiqueDataSource{}

type architectureCritiqueDataSource struct {
	tc *terrapod.Client
}

type architectureCritiqueModel struct {
	WorkspaceID  types.String `tfsdk:"workspace_id"`
	Status       types.String `tfsdk:"status"`
	StateSerial  types.Int64  `tfsdk:"state_serial"`
	RiskLevel    types.String `tfsdk:"risk_level"`
	Architecture types.Object `tfsdk:"architecture"`
	Findings     types.List   `tfsdk:"findings"`
	Deferred     types.List   `tfsdk:"deferred"`
	Model        types.String `tfsdk:"model"`
	InputTokens  types.Int64  `tfsdk:"input_tokens"`
	OutputTokens types.Int64  `tfsdk:"output_tokens"`
	ErrorMessage types.String `tfsdk:"error_message"`
	CreatedAt    types.String `tfsdk:"created_at"`
	UpdatedAt    types.String `tfsdk:"updated_at"`
}

type architectureObj struct {
	Summary     types.String `tfsdk:"summary"`
	Tiers       types.List   `tfsdk:"tiers"`
	DataStores  types.List   `tfsdk:"data_stores"`
	BlastRadius types.String `tfsdk:"blast_radius"`
}

type findingObj struct {
	Severity        types.String `tfsdk:"severity"`
	Category        types.String `tfsdk:"category"`
	Title           types.String `tfsdk:"title"`
	Detail          types.String `tfsdk:"detail"`
	ResourceAddress types.String `tfsdk:"resource_address"`
	Recommendation  types.String `tfsdk:"recommendation"`
	GroundedIn      types.String `tfsdk:"grounded_in"`
}

var architectureAttrTypes = map[string]attr.Type{
	"summary":      types.StringType,
	"tiers":        types.ListType{ElemType: types.StringType},
	"data_stores":  types.ListType{ElemType: types.StringType},
	"blast_radius": types.StringType,
}

var findingAttrTypes = map[string]attr.Type{
	"severity":         types.StringType,
	"category":         types.StringType,
	"title":            types.StringType,
	"detail":           types.StringType,
	"resource_address": types.StringType,
	"recommendation":   types.StringType,
	"grounded_in":      types.StringType,
}

// NewDataSource returns a new architecture-critique data source.
func NewDataSource() datasource.DataSource {
	return &architectureCritiqueDataSource{}
}

func (d *architectureCritiqueDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_architecture_critique"
}

func (d *architectureCritiqueDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "AI architecture critique of a workspace's CURRENT deployed system, inferred " +
			"from its latest Terraform state (the optional ai_architecture feature). Unlike a run's " +
			"plan summary — which reviews a change — this reviews the system as it EXISTS, across " +
			"reliability/security/cost/operations/scalability. Every finding is grounded in " +
			"deterministic data (security ← the Checkov/Trivy scanner; cost ← the cost engine; the " +
			"rest ← state + the resource graph) and anchored to a resource address. Requires the " +
			"feature enabled and a critique generated for the workspace's current state.",
		Attributes: map[string]schema.Attribute{
			"workspace_id": schema.StringAttribute{
				Description: "Workspace ID (ws-… or a bare UUID) to critique.",
				Required:    true,
			},
			"status":       schema.StringAttribute{Description: "ready | pending | skipped | errored.", Computed: true},
			"state_serial": schema.Int64Attribute{Description: "Serial of the state version this critique was generated for.", Computed: true},
			"risk_level":   schema.StringAttribute{Description: "Overall architectural-health grade: low | medium | high | critical.", Computed: true},
			"architecture": schema.SingleNestedAttribute{
				Description: "The model's inference of the system as it exists, grounded in resource addresses.",
				Computed:    true,
				Attributes: map[string]schema.Attribute{
					"summary":      schema.StringAttribute{Description: "2–4 sentences: what kind of system this is, its tiers, data stores, exposure, and blast radius.", Computed: true},
					"tiers":        schema.ListAttribute{Description: "Each tier/component and the resources that make it up.", ElementType: types.StringType, Computed: true},
					"data_stores":  schema.ListAttribute{Description: "Each data store (address) and what it holds.", ElementType: types.StringType, Computed: true},
					"blast_radius": schema.StringAttribute{Description: "Where failure/coupling concentrates (hubs, single-AZ, shared deps).", Computed: true},
				},
			},
			"findings": schema.ListNestedAttribute{
				Description: "Discrete critique items, ranked by real operational risk. A well-architected system yields an empty list.",
				Computed:    true,
				NestedObject: schema.NestedAttributeObject{
					Attributes: map[string]schema.Attribute{
						"severity":         schema.StringAttribute{Description: "low | medium | high | critical.", Computed: true},
						"category":         schema.StringAttribute{Description: "reliability | security | cost | operations | scalability.", Computed: true},
						"title":            schema.StringAttribute{Description: "Short finding title.", Computed: true},
						"detail":           schema.StringAttribute{Description: "Why it matters for THIS system, grounded in state.", Computed: true},
						"resource_address": schema.StringAttribute{Description: "Exact resource address from state the finding anchors to.", Computed: true},
						"recommendation":   schema.StringAttribute{Description: "A specific, actionable change.", Computed: true},
						"grounded_in":      schema.StringAttribute{Description: "Provenance: 'state', a scanner rule id (e.g. 'CKV_AWS_24') for security findings, or 'cost'.", Computed: true},
					},
				},
			},
			"deferred": schema.ListAttribute{
				Description: "Concerns the model could NOT judge from the data given — surfaced so operators see the gaps rather than the model guessing.",
				ElementType: types.StringType,
				Computed:    true,
			},
			"model":         schema.StringAttribute{Description: "Model that produced the critique.", Computed: true},
			"input_tokens":  schema.Int64Attribute{Description: "Prompt tokens consumed.", Computed: true},
			"output_tokens": schema.Int64Attribute{Description: "Completion tokens produced.", Computed: true},
			"error_message": schema.StringAttribute{Description: "Failure reason when status is errored; empty otherwise.", Computed: true},
			"created_at":    schema.StringAttribute{Description: "RFC3339 creation timestamp.", Computed: true},
			"updated_at":    schema.StringAttribute{Description: "RFC3339 last-update timestamp.", Computed: true},
		},
	}
}

func (d *architectureCritiqueDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
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

func (d *architectureCritiqueDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config architectureCritiqueModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	cr, err := d.tc.GetArchitectureCritique(ctx, config.WorkspaceID.ValueString())
	if err != nil {
		resp.Diagnostics.AddError("Failed to read architecture critique", err.Error())
		return
	}

	config.Status = types.StringValue(cr.Status)
	config.StateSerial = types.Int64Value(cr.StateSerial)
	config.RiskLevel = types.StringValue(cr.RiskLevel)
	config.Model = types.StringValue(cr.Model)
	config.InputTokens = types.Int64Value(int64(cr.InputTokens))
	config.OutputTokens = types.Int64Value(int64(cr.OutputTokens))
	config.ErrorMessage = types.StringValue(cr.ErrorMessage)
	config.CreatedAt = types.StringValue(cr.CreatedAt)
	config.UpdatedAt = types.StringValue(cr.UpdatedAt)

	tiers, diags := types.ListValueFrom(ctx, types.StringType, cr.Architecture.Tiers)
	resp.Diagnostics.Append(diags...)
	dataStores, diags := types.ListValueFrom(ctx, types.StringType, cr.Architecture.DataStores)
	resp.Diagnostics.Append(diags...)
	archObj, diags := types.ObjectValueFrom(ctx, architectureAttrTypes, architectureObj{
		Summary:     types.StringValue(cr.Architecture.Summary),
		Tiers:       tiers,
		DataStores:  dataStores,
		BlastRadius: types.StringValue(cr.Architecture.BlastRadius),
	})
	resp.Diagnostics.Append(diags...)
	config.Architecture = archObj

	findings := make([]findingObj, 0, len(cr.Findings))
	for _, f := range cr.Findings {
		findings = append(findings, findingObj{
			Severity:        types.StringValue(f.Severity),
			Category:        types.StringValue(f.Category),
			Title:           types.StringValue(f.Title),
			Detail:          types.StringValue(f.Detail),
			ResourceAddress: types.StringValue(f.ResourceAddress),
			Recommendation:  types.StringValue(f.Recommendation),
			GroundedIn:      types.StringValue(f.GroundedIn),
		})
	}
	findingList, diags := types.ListValueFrom(ctx, types.ObjectType{AttrTypes: findingAttrTypes}, findings)
	resp.Diagnostics.Append(diags...)
	config.Findings = findingList

	deferredList, diags := types.ListValueFrom(ctx, types.StringType, cr.Deferred)
	resp.Diagnostics.Append(diags...)
	config.Deferred = deferredList

	resp.Diagnostics.Append(resp.State.Set(ctx, &config)...)
}
