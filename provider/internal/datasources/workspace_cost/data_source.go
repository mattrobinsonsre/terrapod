// Package workspace_cost implements the terrapod_workspace_cost data source (#871).
//
// API Contract: GET /api/terrapod/v1/workspaces/{id}/cost-estimate
// Reports the current monthly cost of a workspace's managed infrastructure,
// computed server-side from its latest state version by the native
// native cost engine. Data only — no AI. Useful in config for budget
// guardrails, reporting, or feeding a threshold into an alert.
//
// A workspace with no state yet returns a zeroed estimate with a null
// state_version (not an error).
package workspace_cost

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

var _ datasource.DataSource = &workspaceCostDataSource{}

type workspaceCostDataSource struct {
	tc *terrapod.Client
}

type workspaceCostDataSourceModel struct {
	WorkspaceID           types.String  `tfsdk:"workspace_id"`
	Currency              types.String  `tfsdk:"currency"`
	TotalMin              types.Float64 `tfsdk:"total_min"`
	TotalMax              types.Float64 `tfsdk:"total_max"`
	PreviousMin           types.Float64 `tfsdk:"previous_min"`
	PreviousMax           types.Float64 `tfsdk:"previous_max"`
	DiffMin               types.Float64 `tfsdk:"diff_min"`
	DiffMax               types.Float64 `tfsdk:"diff_max"`
	Resources             types.List    `tfsdk:"resources"`
	Unpriced              types.List    `tfsdk:"unpriced"`
	StateVersionID        types.String  `tfsdk:"state_version_id"`
	StateVersionSerial    types.Int64   `tfsdk:"state_version_serial"`
	StateVersionCreatedAt types.String  `tfsdk:"state_version_created_at"`
}

type costResourceObj struct {
	Address          types.String  `tfsdk:"address"`
	Type             types.String  `tfsdk:"type"`
	Name             types.String  `tfsdk:"name"`
	Change           types.String  `tfsdk:"change"`
	MonthlyMin       types.Float64 `tfsdk:"monthly_min"`
	MonthlyMax       types.Float64 `tfsdk:"monthly_max"`
	UsageAssumptions types.List    `tfsdk:"usage_assumptions"`
}

// usageAssumptionObj is the low/typical/high usage band assumed for a resource
// whose cost can't be priced deterministically from state (data processed,
// invocations, storage). Surfaced raw so the estimate is honest with AI off:
// the cost assumes `typical`, and could sit anywhere in `low`–`high`.
type usageAssumptionObj struct {
	Description types.String  `tfsdk:"description"`
	Dimension   types.String  `tfsdk:"dimension"`
	Unit        types.String  `tfsdk:"unit"`
	Low         types.Float64 `tfsdk:"low"`
	Typical     types.Float64 `tfsdk:"typical"`
	High        types.Float64 `tfsdk:"high"`
	// Monthly cost at low/typical/high usage — null when the server couldn't
	// price the band.
	CostLow     types.Float64 `tfsdk:"cost_low"`
	CostTypical types.Float64 `tfsdk:"cost_typical"`
	CostHigh    types.Float64 `tfsdk:"cost_high"`
}

type unpricedObj struct {
	Address types.String `tfsdk:"address"`
	Type    types.String `tfsdk:"type"`
	Change  types.String `tfsdk:"change"`
}

var usageAssumptionAttrTypes = map[string]attr.Type{
	"description":  types.StringType,
	"dimension":    types.StringType,
	"unit":         types.StringType,
	"low":          types.Float64Type,
	"typical":      types.Float64Type,
	"high":         types.Float64Type,
	"cost_low":     types.Float64Type,
	"cost_typical": types.Float64Type,
	"cost_high":    types.Float64Type,
}

var costResourceAttrTypes = map[string]attr.Type{
	"address":           types.StringType,
	"type":              types.StringType,
	"name":              types.StringType,
	"change":            types.StringType,
	"monthly_min":       types.Float64Type,
	"monthly_max":       types.Float64Type,
	"usage_assumptions": types.ListType{ElemType: types.ObjectType{AttrTypes: usageAssumptionAttrTypes}},
}

var unpricedAttrTypes = map[string]attr.Type{
	"address": types.StringType,
	"type":    types.StringType,
	"change":  types.StringType,
}

// float64PtrValue maps an optional API float (nil = "the server didn't price
// this band") to a Terraform Float64, null when absent.
func float64PtrValue(p *float64) types.Float64 {
	if p == nil {
		return types.Float64Null()
	}
	return types.Float64Value(*p)
}

// NewDataSource returns a new workspace-cost data source.
func NewDataSource() datasource.DataSource {
	return &workspaceCostDataSource{}
}

func (d *workspaceCostDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_workspace_cost"
}

func (d *workspaceCostDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Current monthly cost of a workspace's managed infrastructure, computed " +
			"from its latest state by the native native cost engine (data only, no AI). " +
			"Useful for budget guardrails and reporting. A workspace with no state returns a " +
			"zeroed estimate with a null state_version.",
		Attributes: map[string]schema.Attribute{
			"workspace_id": schema.StringAttribute{
				Description: "Workspace ID (ws-… or a bare UUID) to estimate.",
				Required:    true,
			},
			"currency":     schema.StringAttribute{Description: "ISO currency code (e.g. USD).", Computed: true},
			"total_min":    schema.Float64Attribute{Description: "Current monthly total, lower bound.", Computed: true},
			"total_max":    schema.Float64Attribute{Description: "Current monthly total, upper bound (equals total_min for deterministic prices).", Computed: true},
			"previous_min": schema.Float64Attribute{Description: "Prior monthly total, lower bound (equals total for a state estimate).", Computed: true},
			"previous_max": schema.Float64Attribute{Description: "Prior monthly total, upper bound.", Computed: true},
			"diff_min":     schema.Float64Attribute{Description: "Monthly delta, lower bound (zero for a state estimate).", Computed: true},
			"diff_max":     schema.Float64Attribute{Description: "Monthly delta, upper bound (zero for a state estimate).", Computed: true},
			"resources": schema.ListNestedAttribute{
				Description: "Per-resource priced breakdown. Every resource is a `noop` for a state estimate.",
				Computed:    true,
				NestedObject: schema.NestedAttributeObject{
					Attributes: map[string]schema.Attribute{
						"address":     schema.StringAttribute{Description: "Resource address.", Computed: true},
						"type":        schema.StringAttribute{Description: "Resource type.", Computed: true},
						"name":        schema.StringAttribute{Description: "Resource name.", Computed: true},
						"change":      schema.StringAttribute{Description: "noop | add | remove.", Computed: true},
						"monthly_min": schema.Float64Attribute{Description: "Monthly cost, lower bound.", Computed: true},
						"monthly_max": schema.Float64Attribute{Description: "Monthly cost, upper bound.", Computed: true},
						"usage_assumptions": schema.ListNestedAttribute{
							Description: "Usage bands assumed for resources whose cost depends on runtime usage the plan " +
								"doesn't declare (data processed, invocations, storage). The monthly cost assumes " +
								"`typical`; the true cost sits somewhere in `low`–`high`. Empty for deterministically-priced resources.",
							Computed: true,
							NestedObject: schema.NestedAttributeObject{
								Attributes: map[string]schema.Attribute{
									"description":  schema.StringAttribute{Description: "Human-readable label for the assumption.", Computed: true},
									"dimension":    schema.StringAttribute{Description: "What is being metered (e.g. \"data processed\").", Computed: true},
									"unit":         schema.StringAttribute{Description: "Unit of the band bounds (e.g. \"GB/month\").", Computed: true},
									"low":          schema.Float64Attribute{Description: "Low-usage bound (quantity).", Computed: true},
									"typical":      schema.Float64Attribute{Description: "Typical (assumed) usage quantity — the point estimate the monthly cost is based on.", Computed: true},
									"high":         schema.Float64Attribute{Description: "High-usage bound (quantity).", Computed: true},
									"cost_low":     schema.Float64Attribute{Description: "Monthly cost at low usage; null when unpriceable.", Computed: true},
									"cost_typical": schema.Float64Attribute{Description: "Monthly cost at typical usage (the amount folded into `monthly`); null when unpriceable.", Computed: true},
									"cost_high":    schema.Float64Attribute{Description: "Monthly cost at high usage — the honest upper bound if usage runs hot; null when unpriceable.", Computed: true},
								},
							},
						},
					},
				},
			},
			"unpriced": schema.ListNestedAttribute{
				Description: "Resources the pricing data couldn't price (unmapped type, no direct cost, or an uncovered provider).",
				Computed:    true,
				NestedObject: schema.NestedAttributeObject{
					Attributes: map[string]schema.Attribute{
						"address": schema.StringAttribute{Description: "Resource address.", Computed: true},
						"type":    schema.StringAttribute{Description: "Resource type.", Computed: true},
						"change":  schema.StringAttribute{Description: "noop | add | remove.", Computed: true},
					},
				},
			},
			"state_version_id":         schema.StringAttribute{Description: "ID of the priced state version (sv-…); null when the workspace has no state.", Computed: true},
			"state_version_serial":     schema.Int64Attribute{Description: "Serial of the priced state version; 0 when no state.", Computed: true},
			"state_version_created_at": schema.StringAttribute{Description: "Creation timestamp of the priced state version; null when no state.", Computed: true},
		},
	}
}

func (d *workspaceCostDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
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

func (d *workspaceCostDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config workspaceCostDataSourceModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	est, err := d.tc.GetWorkspaceCostEstimate(ctx, config.WorkspaceID.ValueString())
	if err != nil {
		resp.Diagnostics.AddError("Failed to read workspace cost estimate", err.Error())
		return
	}

	config.Currency = types.StringValue(est.Currency)
	config.TotalMin = types.Float64Value(est.Total.Min)
	config.TotalMax = types.Float64Value(est.Total.Max)
	config.PreviousMin = types.Float64Value(est.Previous.Min)
	config.PreviousMax = types.Float64Value(est.Previous.Max)
	config.DiffMin = types.Float64Value(est.Diff.Min)
	config.DiffMax = types.Float64Value(est.Diff.Max)

	resources := make([]costResourceObj, 0, len(est.Resources))
	for _, r := range est.Resources {
		bands := make([]usageAssumptionObj, 0, len(r.UsageAssumptions))
		for _, a := range r.UsageAssumptions {
			bands = append(bands, usageAssumptionObj{
				Description: types.StringValue(a.Description),
				Dimension:   types.StringValue(a.Dimension),
				Unit:        types.StringValue(a.Unit),
				Low:         types.Float64Value(a.Low),
				Typical:     types.Float64Value(a.Typical),
				High:        types.Float64Value(a.High),
				CostLow:     float64PtrValue(a.CostLow),
				CostTypical: float64PtrValue(a.CostTypical),
				CostHigh:    float64PtrValue(a.CostHigh),
			})
		}
		bandList, diags := types.ListValueFrom(ctx, types.ObjectType{AttrTypes: usageAssumptionAttrTypes}, bands)
		resp.Diagnostics.Append(diags...)
		resources = append(resources, costResourceObj{
			Address:          types.StringValue(r.Address),
			Type:             types.StringValue(r.Type),
			Name:             types.StringValue(r.Name),
			Change:           types.StringValue(r.Change),
			MonthlyMin:       types.Float64Value(r.Monthly.Min),
			MonthlyMax:       types.Float64Value(r.Monthly.Max),
			UsageAssumptions: bandList,
		})
	}
	resList, diags := types.ListValueFrom(ctx, types.ObjectType{AttrTypes: costResourceAttrTypes}, resources)
	resp.Diagnostics.Append(diags...)
	config.Resources = resList

	unpriced := make([]unpricedObj, 0, len(est.Unpriced))
	for _, u := range est.Unpriced {
		unpriced = append(unpriced, unpricedObj{
			Address: types.StringValue(u.Address),
			Type:    types.StringValue(u.Type),
			Change:  types.StringValue(u.Change),
		})
	}
	unpricedList, diags := types.ListValueFrom(ctx, types.ObjectType{AttrTypes: unpricedAttrTypes}, unpriced)
	resp.Diagnostics.Append(diags...)
	config.Unpriced = unpricedList

	if est.StateVersion != nil {
		config.StateVersionID = types.StringValue(est.StateVersion.ID)
		config.StateVersionSerial = types.Int64Value(int64(est.StateVersion.Serial))
		config.StateVersionCreatedAt = types.StringValue(est.StateVersion.CreatedAt)
	} else {
		config.StateVersionID = types.StringNull()
		config.StateVersionSerial = types.Int64Value(0)
		config.StateVersionCreatedAt = types.StringNull()
	}

	resp.Diagnostics.Append(resp.State.Set(ctx, &config)...)
}
