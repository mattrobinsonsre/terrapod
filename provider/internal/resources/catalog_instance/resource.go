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

	CatalogItemID      types.String `tfsdk:"catalog_item_id"`
	Name               types.String `tfsdk:"name"`
	AgentPoolID        types.String `tfsdk:"agent_pool_id"`
	InputValues        types.Map    `tfsdk:"input_values"`
	VersionPin         types.String `tfsdk:"version_pin"`
	AutoApply          types.Bool   `tfsdk:"auto_apply"`
	ExecutionBackend   types.String `tfsdk:"execution_backend"`
	EngineVersion      types.String `tfsdk:"engine_version"`
	Labels             types.Map    `tfsdk:"labels"`
	UpdateBlockVersion types.String `tfsdk:"update_block_version"`
	UpdateBlockReason  types.String `tfsdk:"update_block_reason"`
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
				// Sharper than an ordinary missing flag: the server deliberately
				// omits sensitive inputs (write-only, encrypted at rest, never
				// round-tripped), so the prior-state copy kept below makes the
				// PROVIDER the sole durable holder of those secrets. Unmarked, it
				// held them in plaintext — the server does the right thing and
				// this undid it (GHSA-9646-883f-wjjm).
				Sensitive: true,
			},
			"version_pin": schema.StringAttribute{
				Description: "Module version pin to provision/reconfigure to (e.g. \"~> 1.0\").",
				Optional:    true, Computed: true, PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"auto_apply": schema.BoolAttribute{
				Description: "Whether provision and reconfigure runs auto-apply. Defaults to false.",
				Optional:    true,
				Computed:    true,
				Default:     booldefault.StaticBool(false),
			},
			"execution_backend": schema.StringAttribute{
				Description: "The engine this instance runs on — \"tofu\" or \"terraform\". " +
					"Omit to take the deployment's default; note that a catalog item with " +
					"execution_backend_constraints validates that default too, so an item " +
					"whose constraints exclude it cannot be provisioned without naming a " +
					"permitted backend here.",
				Optional: true, Computed: true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"engine_version": schema.StringAttribute{
				Description: "The engine version for this instance. The server RESOLVES this " +
					"to an exact offered binary version, so a partial value like \"1.12\" is " +
					"a request rather than the stored result; this attribute keeps what you " +
					"configured and reports the resolved version only when you configure " +
					"nothing. Refused when it falls outside the item's declared range.",
				Optional: true, Computed: true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"labels": schema.MapAttribute{
				Description: "Labels for the provisioned workspace. Changing this forces a new instance.",
				Optional:    true,
				ElementType: types.StringType,
				PlanModifiers: []planmodifier.Map{
					mapplanmodifier.RequiresReplace(),
				},
			},
			"update_block_version": schema.StringAttribute{
				Description: "Version whose publish requires attention; null when no update is blocked.",
				Computed:    true,
			},
			"update_block_reason": schema.StringAttribute{
				Description: "Non-sensitive reason code for a blocked catalog publish.",
				Computed:    true,
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

	// Read the provision response back into the model rather than persisting the
	// plan as it stands. version_pin, execution_backend and engine_version are
	// all Optional + Computed with no Default, so a config that omits them --
	// the documented usage, "omit to take the deployment's default" -- plans
	// UNKNOWN, and UseStateForUnknown cannot fire on a create because there is
	// no prior state. Persisting the plan therefore left three unknowns in state
	// and Terraform refused the result: "the provider still indicated an unknown
	// value", once per attribute, on every apply. The instance was provisioned
	// server-side first, so each attempt orphaned a workspace and left the
	// resource tainted -- which makes the next apply destroy and recreate it and
	// fail identically. Every sibling resource reads back; this one was the
	// deviation (#2425 section 1.1).
	//
	// The shared readback is correct here and that is a property of the provision
	// response rather than an accident: measured against a live instance, it
	// carries name, agent-pool-id, labels, catalog-update-block,
	// catalog-version-pin, execution-backend and engine-version. It omits
	// input-values and auto-apply, which the readback treats as "keep what the
	// caller configured" -- the right answer on a create. And a CONFIGURED
	// engine_version must not be overwritten: the server resolves a partial
	// value ("1.13" came back as "1.13.9"), so the readback's guard, which takes
	// the server's answer only when nothing was configured, is what keeps this
	// from being the inconsistent-result failure in a different attribute.
	resp.Diagnostics.Append(readCatalogInstanceIntoModel(ctx, inst, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
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
	plan.UpdateBlockVersion = types.StringNull()
	plan.UpdateBlockReason = types.StringNull()
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
	// Both paths accept these, so they belong in the shared builder rather
	// than only on provision (#2372).
	if !m.ExecutionBackend.IsNull() && !m.ExecutionBackend.IsUnknown() {
		attrs["execution-backend"] = m.ExecutionBackend.ValueString()
	}
	if !m.EngineVersion.IsNull() && !m.EngineVersion.IsUnknown() {
		attrs["engine-version"] = m.EngineVersion.ValueString()
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
	m.UpdateBlockVersion = types.StringNull()
	m.UpdateBlockReason = types.StringNull()
	if inst.UpdateBlock != nil {
		m.UpdateBlockVersion = types.StringValue(inst.UpdateBlock["version"])
		m.UpdateBlockReason = types.StringValue(inst.UpdateBlock["reason"])
	}

	if name := attrString(inst.Attributes, "name"); name != "" {
		m.Name = types.StringValue(name)
	}
	if pool := attrString(inst.Attributes, "agent-pool-id"); pool != "" {
		m.AgentPoolID = types.StringValue(pool)
	}

	// input_values in state is the set the PRACTITIONER manages, and the server's
	// is neither a subset nor a superset of it, so this is an overlay in both
	// directions rather than a replace.
	//
	// It omits sensitive inputs, which are write-only — encrypted at rest, never
	// round-tripped, like TFE sensitive variables. A wholesale replace would
	// drop every sensitive key from state, producing a perpetual plan diff and a
	// needless re-apply each run. So a prior-state key the server does not
	// return is KEPT.
	//
	// And it ADDS the module's own variable defaults, which it materialises as
	// workspace variables — measured: a module declaring
	// `variable "name" { default = "probe" }` reports `name=probe` on an
	// instance whose configuration set no inputs at all. Copying the map
	// wholesale therefore introduced keys no configuration has, and because
	// input_values is Optional (not Computed) the next plan wanted them gone: a
	// permanent in-place update, on every plan, for any module whose variables
	// have defaults — which is most of them, and the only workaround was to
	// restate every default in the configuration (#2425 section 1.1). So a
	// server key the state does not hold is IGNORED.
	//
	// The cost is symmetric and is the reason both halves are safe: neither an
	// input added out of band nor one removed out of band is detected on
	// refresh, because a catalog workspace's variables are config-managed and
	// only mutated through reconfigure.
	merged := map[string]string{}
	if !m.InputValues.IsNull() && !m.InputValues.IsUnknown() {
		diags.Append(m.InputValues.ElementsAs(ctx, &merged, false)...)
	}
	for key, served := range attrStringMap(inst.Attributes, "input-values") {
		if _, held := merged[key]; held {
			merged[key] = served
		}
	}
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
	m.VersionPin = types.StringValue(attrString(inst.Attributes, "catalog-version-pin"))

	if v, ok := inst.Attributes["auto-apply"]; ok && v != nil {
		if b, ok := v.(bool); ok {
			m.AutoApply = types.BoolValue(b)
		}
	}

	// The backend is stored as given, so reading it back is safe and is what
	// detects an out-of-band move onto another engine.
	if backend := attrString(inst.Attributes, "execution-backend"); backend != "" {
		m.ExecutionBackend = types.StringValue(backend)
	}

	// The VERSION is not stored as given: the server resolves a partial like
	// "1.12" to an exact offered binary. Overwriting a CONFIGURED value with
	// the resolved one is the "Provider produced inconsistent result after
	// apply" failure the v1.9.0 advisories were about, so the configured value
	// stands and the server's answer is reported only when nothing was
	// configured. The cost, stated rather than hidden: drift of a configured
	// version is not detected. The workspace resource can read its own
	// engine_version back because the workspace API stores that string
	// verbatim -- this path differs, so it does not copy that shape.
	if m.EngineVersion.IsNull() || m.EngineVersion.IsUnknown() {
		if version := attrString(inst.Attributes, "engine-version"); version != "" {
			m.EngineVersion = types.StringValue(version)
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
