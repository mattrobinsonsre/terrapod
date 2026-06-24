// Package catalog_instance implements the terrapod_catalog_instance
// resource (service catalog, #535) — provisioning a catalog item via
// Terraform.
//
// API Contract (Terrapod API <-> Terraform Provider):
//
//	JSON:API type: "catalog-instances"
//	ID format: the provisioned workspace UUID
//	Create:  POST   /api/terrapod/v1/catalog-items/{item}/provision
//	Read:    GET    /api/terrapod/v1/catalog-instances/{ws}
//	Update:  PATCH  /api/terrapod/v1/catalog-instances/{ws}   (reconfigure)
//	Delete:  POST   /api/terrapod/v1/catalog-instances/{ws}/destroy
//
// Lifecycle mapping:
//
//	Create -> ProvisionCatalogItem(catalog_item_id, attrs); the returned
//	    instance ID (the workspace id) is stored as `id`.
//	Read   -> GetCatalogInstance(id); 404 removes the resource from state.
//	Update -> ReconfigureCatalogInstance(id, attrs) — only input_values,
//	    version_pin, and auto_apply are reconfigurable. catalog_item_id,
//	    name, agent_pool_id, and labels are ForceNew (reconfigure can't move
//	    them).
//	Delete -> DestroyCatalogInstance(id, {auto-apply:true}); on a successful
//	    apply the workspace is archived.
//
// Attribute mapping (JSON:API attribute -> Terraform schema attribute):
//
//	"name"          -> name             (string, required, ForceNew)
//	"agent-pool-id" -> agent_pool_id    (string, required, ForceNew)
//	"input-values"  -> input_values     (map[string]string, optional)
//	"version-pin"   -> version_pin      (string, optional)
//	"auto-apply"    -> auto_apply       (bool, optional, default false)
//	"labels"        -> labels           (map[string]string, optional, ForceNew)
//	(catalog_item_id is the parent item ID, ForceNew, not a server attribute)
//
// Import: by catalog instance (workspace) ID.
package catalog_instance

import (
	"context"
	"fmt"
	"maps"

	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/mapplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var (
	_ resource.Resource                = &catalogInstanceResource{}
	_ resource.ResourceWithImportState = &catalogInstanceResource{}
)

type catalogInstanceModel struct {
	ID types.String `tfsdk:"id"`

	CatalogItemID types.String `tfsdk:"catalog_item_id"`
	Name          types.String `tfsdk:"name"`
	AgentPoolID   types.String `tfsdk:"agent_pool_id"`
	InputValues   types.Map    `tfsdk:"input_values"`
	VersionPin    types.String `tfsdk:"version_pin"`
	AutoApply     types.Bool   `tfsdk:"auto_apply"`
	Labels        types.Map    `tfsdk:"labels"`
}

type catalogInstanceResource struct {
	client *client.Client
	tc     *terrapod.Client
}

// NewResource returns a new catalog instance resource.
func NewResource() resource.Resource {
	return &catalogInstanceResource{}
}

func (r *catalogInstanceResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_catalog_instance"
}

func (r *catalogInstanceResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Provisions a Terrapod service-catalog item — creating a " +
			"catalog-managed workspace from a blessed module without writing the " +
			"Terraform yourself. Reconfigure (input values / version pin / auto-apply) " +
			"queues a run; destroy queues a destroy run and archives the workspace. " +
			"See https://github.com/mattrobinsonsre/terrapod/blob/main/docs/service-catalog.md.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description: "The provisioned workspace ID.",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"catalog_item_id": schema.StringAttribute{
				Description: "The catalog item ID to provision from. Changing this forces a new instance.",
				Required:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"name": schema.StringAttribute{
				Description: "Name for the provisioned workspace. Changing this forces a new instance.",
				Required:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"agent_pool_id": schema.StringAttribute{
				Description: "Agent pool the instance provisions onto. Changing this forces a new instance.",
				Required:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"input_values": schema.MapAttribute{
				Description: "Input variable values for the module (reconfigurable).",
				Optional:    true,
				ElementType: types.StringType,
			},
			"version_pin": schema.StringAttribute{
				Description: "Module version pin to provision/reconfigure to (e.g. \"~> 1.0\").",
				Optional:    true,
			},
			"auto_apply": schema.BoolAttribute{
				Description: "Whether provision and reconfigure runs auto-apply. Defaults to false.",
				Optional:    true,
				Computed:    true,
				Default:     booldefault.StaticBool(false),
			},
			"labels": schema.MapAttribute{
				Description: "Labels for the provisioned workspace. Changing this forces a new instance.",
				Optional:    true,
				ElementType: types.StringType,
				PlanModifiers: []planmodifier.Map{
					mapplanmodifier.RequiresReplace(),
				},
			},
		},
	}
}

func (r *catalogInstanceResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	c, ok := req.ProviderData.(*client.Client)
	if !ok {
		resp.Diagnostics.AddError("Unexpected provider data type", fmt.Sprintf("Expected *client.Client, got %T", req.ProviderData))
		return
	}
	r.client = c

	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: c.BaseURL, Token: c.Token})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	r.tc = tc
}

func (r *catalogInstanceResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan catalogInstanceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs := buildProvisionAttrs(&plan)

	inst, err := r.tc.ProvisionCatalogItem(ctx, plan.CatalogItemID.ValueString(), attrs)
	if err != nil {
		resp.Diagnostics.AddError("Failed to provision catalog instance", err.Error())
		return
	}

	plan.ID = types.StringValue(inst.ID)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *catalogInstanceResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state catalogInstanceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	inst, err := r.tc.GetCatalogInstance(ctx, state.ID.ValueString())
	if err != nil {
		if terrapod.IsNotFound(err) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Failed to read catalog instance", err.Error())
		return
	}

	resp.Diagnostics.Append(readCatalogInstanceIntoModel(ctx, inst, &state)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *catalogInstanceResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan catalogInstanceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var state catalogInstanceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs := buildReconfigureAttrs(&plan)

	if _, err := r.tc.ReconfigureCatalogInstance(ctx, state.ID.ValueString(), attrs); err != nil {
		resp.Diagnostics.AddError("Failed to reconfigure catalog instance", err.Error())
		return
	}

	// The reconfigure response is a run reference, not the instance; carry the
	// provisioned id forward and persist the planned configuration.
	plan.ID = state.ID
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *catalogInstanceResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state catalogInstanceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	_, err := r.tc.DestroyCatalogInstance(ctx, state.ID.ValueString(), map[string]any{"auto-apply": true})
	if err != nil && !terrapod.IsNotFound(err) {
		resp.Diagnostics.AddError("Failed to destroy catalog instance", err.Error())
	}
}

func (r *catalogInstanceResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

// buildProvisionAttrs builds the provision payload. name + agent-pool-id are
// required; input-values / version-pin / auto-apply / labels are sent when set.
func buildProvisionAttrs(m *catalogInstanceModel) map[string]any {
	attrs := map[string]any{
		"name":          m.Name.ValueString(),
		"agent-pool-id": m.AgentPoolID.ValueString(),
	}
	addInstanceCommon(m, attrs)
	if !m.Labels.IsNull() && !m.Labels.IsUnknown() {
		attrs["labels"] = mapToStrings(m.Labels)
	}
	return attrs
}

// buildReconfigureAttrs builds the reconfigure payload — only the mutable
// fields (input-values, version-pin, auto-apply).
func buildReconfigureAttrs(m *catalogInstanceModel) map[string]any {
	attrs := map[string]any{}
	addInstanceCommon(m, attrs)
	return attrs
}

// addInstanceCommon adds the reconfigurable fields shared by provision and
// reconfigure payloads.
func addInstanceCommon(m *catalogInstanceModel, attrs map[string]any) {
	if !m.InputValues.IsNull() && !m.InputValues.IsUnknown() {
		attrs["input-values"] = mapToStrings(m.InputValues)
	}
	if !m.VersionPin.IsNull() && !m.VersionPin.IsUnknown() {
		attrs["version-pin"] = m.VersionPin.ValueString()
	}
	if !m.AutoApply.IsNull() && !m.AutoApply.IsUnknown() {
		attrs["auto-apply"] = m.AutoApply.ValueBool()
	}
}

// mapToStrings flattens a types.Map of strings into a map[string]string.
func mapToStrings(m types.Map) map[string]string {
	out := map[string]string{}
	for k, v := range m.Elements() {
		out[k] = v.(types.String).ValueString()
	}
	return out
}

// readCatalogInstanceIntoModel refreshes the mutable, server-observable
// fields from the SDK shape. ForceNew fields (catalog_item_id, name,
// agent_pool_id, labels) are left as-is in state — they can't change without
// replacement, and the instance read does not authoritatively round-trip the
// originating item ID.
func readCatalogInstanceIntoModel(ctx context.Context, inst *terrapod.CatalogInstance, m *catalogInstanceModel) diag.Diagnostics {
	var diags diag.Diagnostics

	m.ID = types.StringValue(inst.ID)

	if name := attrString(inst.Attributes, "name"); name != "" {
		m.Name = types.StringValue(name)
	}
	if pool := attrString(inst.Attributes, "agent-pool-id"); pool != "" {
		m.AgentPoolID = types.StringValue(pool)
	}

	// Merge the server-returned inputs OVER the prior state rather than
	// replacing wholesale. The server's input-values deliberately OMIT
	// sensitive inputs (they're write-only — encrypted at rest, never
	// round-tripped, like TFE sensitive variables). A wholesale replace would
	// drop every sensitive key from state, producing a perpetual plan diff and
	// a needless re-apply each run. So we keep prior-state keys the server
	// didn't return (the sensitive ones) and overlay the non-sensitive values
	// the server does report. (Tradeoff: an externally-removed non-sensitive
	// key isn't detected on refresh — acceptable, since catalog workspaces are
	// config-managed and their variables are only mutated via reconfigure.)
	merged := map[string]string{}
	if !m.InputValues.IsNull() && !m.InputValues.IsUnknown() {
		diags.Append(m.InputValues.ElementsAs(ctx, &merged, false)...)
	}
	maps.Copy(merged, attrStringMap(inst.Attributes, "input-values"))
	if len(merged) > 0 {
		mv, d := types.MapValueFrom(ctx, types.StringType, merged)
		diags.Append(d...)
		m.InputValues = mv
	} else {
		m.InputValues = types.MapNull(types.StringType)
	}

	// The instance RESPONSE carries the pin as "catalog-version-pin"
	// (the bare "version-pin" is an inbound provision/reconfigure attribute
	// only). Reading the wrong key here caused perpetual plan drift.
	if pin := attrString(inst.Attributes, "catalog-version-pin"); pin != "" {
		m.VersionPin = types.StringValue(pin)
	} else {
		m.VersionPin = types.StringNull()
	}

	if v, ok := inst.Attributes["auto-apply"]; ok && v != nil {
		if b, ok := v.(bool); ok {
			m.AutoApply = types.BoolValue(b)
		}
	}

	return diags
}

// attrString reads a string attribute out of an SDK Attributes map,
// returning "" when absent or not a string.
func attrString(attrs map[string]any, key string) string {
	if v, ok := attrs[key]; ok && v != nil {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

// attrStringMap reads a map[string]string attribute out of an SDK
// Attributes map, coercing values to strings. Returns nil when absent/empty.
func attrStringMap(attrs map[string]any, key string) map[string]string {
	v, ok := attrs[key]
	if !ok || v == nil {
		return nil
	}
	raw, ok := v.(map[string]any)
	if !ok || len(raw) == 0 {
		return nil
	}
	out := make(map[string]string, len(raw))
	for k, val := range raw {
		if s, ok := val.(string); ok {
			out[k] = s
		}
	}
	return out
}
