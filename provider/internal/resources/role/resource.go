// Package role implements the terrapod_role resource.
//
// API Contract (Terrapod API ↔ Terraform Provider):
//
//	JSON:API type: "roles"
//	ID: role name (string, not UUID)
//	Create:  POST   /api/v1/roles
//	Read:    GET    /api/v1/roles/{name}
//	Update:  PATCH  /api/v1/roles/{name}
//	Delete:  DELETE /api/v1/roles/{name}
//
// Migrated to go-terrapod (#347). Roles use "name" at the data
// envelope level instead of "id" — the SDK absorbs that quirk so the
// provider doesn't need a custom marshaller.
package role

import (
	"context"
	"errors"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/boolplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
	"github.com/mattrobinsonsre/terrapod/provider/internal/planmods"
	"github.com/mattrobinsonsre/terrapod/provider/internal/stateupgrade"
)

var (
	_ resource.Resource                 = &roleResource{}
	_ resource.ResourceWithImportState  = &roleResource{}
	_ resource.ResourceWithUpgradeState = &roleResource{}
)

type roleModel struct {
	ID types.String `tfsdk:"id"`

	Name                types.String `tfsdk:"name"`
	Description         types.String `tfsdk:"description"`
	AllowAll            types.Bool   `tfsdk:"allow_all"`
	AllowLabels         types.Map    `tfsdk:"allow_labels"`
	AllowNames          types.List   `tfsdk:"allow_names"`
	DenyLabels          types.Map    `tfsdk:"deny_labels"`
	DenyNames           types.List   `tfsdk:"deny_names"`
	WorkspacePermission types.String `tfsdk:"workspace_permission"`
	PoolPermission      types.String `tfsdk:"pool_permission"`
	RegistryPermission  types.String `tfsdk:"registry_permission"`
	CatalogPermission   types.String `tfsdk:"catalog_permission"`
	Capabilities        types.Set    `tfsdk:"capabilities"`

	BuiltIn   types.Bool   `tfsdk:"built_in"`
	CreatedAt types.String `tfsdk:"created_at"`
	UpdatedAt types.String `tfsdk:"updated_at"`
}

type roleResource struct {
	client *client.Client
	tc     *terrapod.Client
}

func NewResource() resource.Resource {
	return &roleResource{}
}

func (r *roleResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_role"
}

func (r *roleResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = currentSchema()
}

// schemaVersion is 1 because `allow_labels` and `deny_labels` changed from
// map(string) to map(list(string)) in #1480. State written by a 1.x provider
// cannot be read by this schema -- every plan fails with
// `Unable to Read Previously Saved State for UpgradeResourceState` -- so the
// provider upgrades it itself (#2452).
const schemaVersion = 1

func currentSchema() schema.Schema {
	return schema.Schema{
		Version:     schemaVersion,
		Description: "Manages a custom RBAC role in Terrapod.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description: "The role name (used as ID).",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"name": schema.StringAttribute{
				Description: "The role name. Changing this forces a new resource.",
				Required:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"description": schema.StringAttribute{
				Description: "Human-readable description of the role.",
				Optional:    true, Computed: true, PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"allow_all": schema.BoolAttribute{
				Description: "An estate-wide grant: this role's allow side matches EVERY resource on every axis. Deny rules still take precedence, so 'everything except the sealed ones' remains expressible, and it does not raise the role's permission level -- a role granting read still grants read, just everywhere. Use it instead of putting a shared label on every workspace: label and name rules are exact-match, so a workspace created without that label would silently fall outside the role. Defaults to false.",
				Optional:    true,
				Computed:    true,
			},
			// A map of LISTS: several accepted values may bind to one key, so
			// { env = ["prod", "stg"] } reads as "env is prod OR stg". The
			// server has always enforced that shape; this was a map of plain
			// strings until 2.0, which could neither express it nor read a role
			// that used it (#1457).
			"allow_labels": schema.MapAttribute{
				Description: "Labels that grant this role's permission to matching workspaces. " +
					"Each key maps to a list of accepted values, matched as OR.",
				Optional:    true,
				ElementType: types.ListType{ElemType: types.StringType},
			},
			"allow_names": schema.ListAttribute{
				Description: "Workspace names that this role grants access to.",
				Optional:    true,
				ElementType: types.StringType,
			},
			"deny_labels": schema.MapAttribute{
				Description: "Labels that deny this role's permission from matching workspaces. " +
					"Each key maps to a list of values, matched as OR.",
				Optional:    true,
				ElementType: types.ListType{ElemType: types.StringType},
			},
			"deny_names": schema.ListAttribute{
				Description: "Workspace names that this role denies access to.",
				Optional:    true,
				ElementType: types.StringType,
			},
			// The four *_permission level fields are Optional+Computed
			// (#585). When `capabilities` is authored the server treats the
			// explicit capability set as the stored truth and DERIVES these
			// levels as a summary — which is a preset name OR the literal
			// string "custom" when the caps match no preset. A user who
			// authors capabilities won't have written these fields (or wrote
			// values that won't match the derived summary), so they must be
			// Computed to accept the server-returned value (including
			// "custom") without a permanent plan diff. They stay Optional so
			// authoring by level (the common case) still works. No validators
			// restrict them to read/plan/write/admin — the read path can now
			// legitimately return "custom", so an OneOf validator would
			// wrongly reject a capability-authored role's derived level.
			"workspace_permission": schema.StringAttribute{
				Description: "Workspace permission level: read, plan, write, or admin. Server-derived (may be \"custom\") when capabilities are authored.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"pool_permission": schema.StringAttribute{
				Description: "Agent pool permission level: read, write, or admin. Defaults to read. Server-derived (may be \"custom\") when capabilities are authored.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"registry_permission": schema.StringAttribute{
				Description: "Registry permission level for modules and providers: read, write, or admin. Defaults to read. Server-derived (may be \"custom\") when capabilities are authored.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"catalog_permission": schema.StringAttribute{
				Description: "Service-catalog permission level: none, read, use, or admin. Opt-in (no everyone floor); defaults to none. Server-derived (may be \"custom\") when capabilities are authored.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			// capabilities (#585) — explicit "resource:verb" tokens. Chosen as
			// Optional+Computed (the cleaner choice, per the issue): when the
			// user authors it, it is the stored truth and the level fields
			// above become a derived summary; when the user authors by LEVEL
			// instead, the server still returns the effective (expanded)
			// capability set for that level, and Computed lets that expanded
			// set land in state without a diff.
			//
			// It carried `UseStateForUnknown`, which is WRONG for the
			// level-authored half and was #2502: that modifier made the set
			// known in the plan, so an update sent the PRIOR DERIVED set, the
			// server read it as authored and stored it verbatim, and the level
			// edit was dropped. `UnknownSetWhenSourceChanges` keeps the value
			// stable across plans the same way, EXCEPT when the configuration
			// moves one of the four levels — then the prior set is stale by
			// definition and the plan leaves it unknown to be re-derived.
			"capabilities": schema.SetAttribute{
				Description: "Explicit capability set as \"resource:verb\" tokens. When authored it is the source of truth and the *_permission levels become a server-derived summary (possibly \"custom\"). When omitted, reflects the effective capabilities the authored levels expand to.",
				Optional:    true,
				Computed:    true,
				ElementType: types.StringType,
				PlanModifiers: []planmodifier.Set{
					planmods.UnknownSetWhenSourceChanges(
						"workspace_permission",
						"pool_permission",
						"registry_permission",
						"catalog_permission",
					),
				},
			},

			"built_in": schema.BoolAttribute{
				Description: "Whether this is a built-in role.",
				Computed:    true,
				PlanModifiers: []planmodifier.Bool{
					boolplanmodifier.UseStateForUnknown(),
				},
			},
			"created_at": schema.StringAttribute{
				Description: "Creation timestamp.",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"updated_at": schema.StringAttribute{
				Description: "Last update timestamp.",
				Computed:    true,
			},
		},
	}
}

// labelAttributes are the two whose element type changed in #1480.
var labelAttributes = []string{"allow_labels", "deny_labels"}

// priorSchemaV0 is `currentSchema` with those two put back the way version 0
// had them. See `stateupgrade` for why it is derived rather than written out,
// and `TestThePriorSchemaStillDescribesVersionZero` for the guard that keeps
// the derivation honest.
func priorSchemaV0() *schema.Schema {
	return stateupgrade.PriorSchemaWithScalarLabels(currentSchema(), labelAttributes...)
}

func (r *roleResource) UpgradeState(_ context.Context) map[int64]resource.StateUpgrader {
	return map[int64]resource.StateUpgrader{
		0: {
			PriorSchema:   priorSchemaV0(),
			StateUpgrader: upgradeRoleV0toV1,
		},
	}
}

// upgradeRoleV0toV1 rewrites each label value `v` as the one-element list
// `[v]`. Every other attribute passes through untouched, so a 1.x state plans
// as a no-op against a configuration updated to one-element lists.
func upgradeRoleV0toV1(ctx context.Context, req resource.UpgradeStateRequest, resp *resource.UpgradeStateResponse) {
	var prior roleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &prior)...)
	if resp.Diagnostics.HasError() {
		return
	}

	prior.AllowLabels = stateupgrade.WrapLabelValues(ctx, prior.AllowLabels, &resp.Diagnostics)
	prior.DenyLabels = stateupgrade.WrapLabelValues(ctx, prior.DenyLabels, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}

	resp.Diagnostics.Append(resp.State.Set(ctx, prior)...)
}

func (r *roleResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *roleResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan roleModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	role, err := r.tc.CreateRole(ctx, buildCreateRoleRequest(&plan))
	if err != nil {
		resp.Diagnostics.AddError("Failed to create role", err.Error())
		return
	}

	resp.Diagnostics.Append(readRoleFromSDK(ctx, role, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *roleResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state roleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	role, err := r.tc.GetRole(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Failed to read role", err.Error())
		return
	}

	resp.Diagnostics.Append(readRoleFromSDK(ctx, role, &state)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *roleResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan roleModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var state roleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	// The CONFIGURATION, not just the plan: the four level attributes are
	// Optional+Computed, so the plan cannot distinguish a level the
	// practitioner authored from one the server derived (#2502).
	var cfg roleModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &cfg)...)
	if resp.Diagnostics.HasError() {
		return
	}

	role, err := r.tc.UpdateRole(ctx, state.ID.ValueString(), buildUpdateRoleRequest(&plan, &cfg))
	if err != nil {
		resp.Diagnostics.AddError("Failed to update role", err.Error())
		return
	}

	resp.Diagnostics.Append(readRoleFromSDK(ctx, role, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *roleResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state roleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	err := r.tc.DeleteRole(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Failed to delete role", err.Error())
		}
	}
}

func (r *roleResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), req.ID)...)
}

func buildCreateRoleRequest(m *roleModel) terrapod.CreateRoleRequest {
	req := terrapod.CreateRoleRequest{
		Name:                m.Name.ValueString(),
		WorkspacePermission: m.WorkspacePermission.ValueString(),
	}
	if !m.PoolPermission.IsNull() && !m.PoolPermission.IsUnknown() {
		req.PoolPermission = m.PoolPermission.ValueString()
	}
	if !m.RegistryPermission.IsNull() && !m.RegistryPermission.IsUnknown() {
		req.RegistryPermission = m.RegistryPermission.ValueString()
	}
	if !m.CatalogPermission.IsNull() && !m.CatalogPermission.IsUnknown() {
		req.CatalogPermission = m.CatalogPermission.ValueString()
	}
	// Only send capabilities when the user authored them. Because the
	// attribute is Computed, an unauthored value comes through as Unknown
	// (never Null) on Create — guard on both so we never send the derived
	// set back as if it were authored.
	if !m.Capabilities.IsNull() && !m.Capabilities.IsUnknown() {
		req.Capabilities = sliceFromTFSet(m.Capabilities)
	}
	if !m.Description.IsNull() {
		req.Description = m.Description.ValueString()
	}
	if !m.AllowAll.IsNull() && !m.AllowAll.IsUnknown() {
		req.AllowAll = m.AllowAll.ValueBool()
	}
	if !m.AllowLabels.IsNull() && !m.AllowLabels.IsUnknown() {
		req.AllowLabels = mapFromTFMap(m.AllowLabels)
	}
	if !m.AllowNames.IsNull() && !m.AllowNames.IsUnknown() {
		req.AllowNames = sliceFromTFList(m.AllowNames)
	}
	if !m.DenyLabels.IsNull() && !m.DenyLabels.IsUnknown() {
		req.DenyLabels = mapFromTFMap(m.DenyLabels)
	}
	if !m.DenyNames.IsNull() && !m.DenyNames.IsUnknown() {
		req.DenyNames = sliceFromTFList(m.DenyNames)
	}
	return req
}

// buildUpdateRoleRequest — Terraform always passes every attribute on
// Update (plan diff is the framework's job), so every present field
// becomes a pointer. Null/unknown attributes leave the underlying
// pointer nil so the SDK omits them from the PATCH body.
func buildUpdateRoleRequest(m, cfg *roleModel) terrapod.UpdateRoleRequest {
	req := terrapod.UpdateRoleRequest{}
	// A level is sent only when the CONFIGURATION names it (#2502). An
	// unauthored level holds the server's own derived summary of the stored
	// capability set, and sending a summary back as an instruction is the same
	// defect this issue is about, one field over. It is also not always
	// expressible: a set matching no preset summarises to the literal
	// "custom", which is not a level the server can expand, so sending it back
	// is a 422 on an axis the practitioner never touched. Omitting it instead
	// is what makes the server's own partial-level semantics reachable --
	// `_apply_level_edits` replaces only the axes a request names and
	// preserves granular capabilities on the rest.
	for _, lvl := range []struct {
		authored, planned types.String
		into              *string
	}{
		{cfg.WorkspacePermission, m.WorkspacePermission, &req.WorkspacePermission},
		{cfg.PoolPermission, m.PoolPermission, &req.PoolPermission},
		{cfg.RegistryPermission, m.RegistryPermission, &req.RegistryPermission},
		{cfg.CatalogPermission, m.CatalogPermission, &req.CatalogPermission},
	} {
		if lvl.authored.IsNull() {
			continue
		}
		if lvl.planned.IsNull() || lvl.planned.IsUnknown() {
			continue
		}
		*lvl.into = lvl.planned.ValueString()
	}
	// Send capabilities on update only when authored (non-null, known). An
	// unauthored capabilities field plans as Unknown -- which is what
	// `UnknownSetWhenSourceChanges` on the schema now guarantees when a level
	// moves -- so the previously-derived set is never pinned as authored.
	if !m.Capabilities.IsNull() && !m.Capabilities.IsUnknown() {
		req.Capabilities = sliceFromTFSet(m.Capabilities)
	}
	if !m.Description.IsNull() && !m.Description.IsUnknown() {
		d := m.Description.ValueString()
		req.Description = &d
	}
	// Allow/deny — null on the model ⇒ empty pointer (clear); set ⇒
	// pointer to the materialised value. This matches the old
	// behaviour where the resource always wrote allow/deny to the API
	// (omitting allow_labels in HCL cleared them on the server).
	allowAll := m.AllowAll.ValueBool()
	req.AllowAll = &allowAll
	allowLabels := mapFromTFMapOrEmpty(m.AllowLabels)
	req.AllowLabels = &allowLabels
	allowNames := sliceFromTFListOrEmpty(m.AllowNames)
	req.AllowNames = &allowNames
	denyLabels := mapFromTFMapOrEmpty(m.DenyLabels)
	req.DenyLabels = &denyLabels
	denyNames := sliceFromTFListOrEmpty(m.DenyNames)
	req.DenyNames = &denyNames
	return req
}

func readRoleFromSDK(ctx context.Context, role *terrapod.Role, m *roleModel) diag.Diagnostics {
	var diags diag.Diagnostics

	m.ID = types.StringValue(role.Name)
	m.Name = types.StringValue(role.Name)

	m.WorkspacePermission = types.StringValue(role.WorkspacePermission)
	if role.PoolPermission != "" {
		m.PoolPermission = types.StringValue(role.PoolPermission)
	} else {
		m.PoolPermission = types.StringValue("read")
	}
	if role.RegistryPermission != "" {
		m.RegistryPermission = types.StringValue(role.RegistryPermission)
	} else {
		m.RegistryPermission = types.StringValue("read")
	}
	if role.CatalogPermission != "" {
		m.CatalogPermission = types.StringValue(role.CatalogPermission)
	} else {
		m.CatalogPermission = types.StringValue("none")
	}

	// capabilities always come back from the server (the effective set,
	// whether authored directly or expanded from the levels). Setting a
	// non-null value here — even when the user authored by level — is what
	// keeps the Computed attribute from perpetually diffing.
	if len(role.Capabilities) > 0 {
		val, d := types.SetValueFrom(ctx, types.StringType, role.Capabilities)
		diags.Append(d...)
		m.Capabilities = val
	} else {
		m.Capabilities = types.SetNull(types.StringType)
	}

	m.BuiltIn = types.BoolValue(role.BuiltIn)
	m.CreatedAt = types.StringValue(role.CreatedAt)
	m.UpdatedAt = types.StringValue(role.UpdatedAt)

	m.Description = types.StringValue(role.Description)

	// Optional+Computed: always set from the server, so a config that omits it
	// takes the server's false rather than drifting forever (#684).
	m.AllowAll = types.BoolValue(role.AllowAll)
	if len(role.AllowLabels) > 0 {
		val, d := types.MapValueFrom(ctx, types.ListType{ElemType: types.StringType}, role.AllowLabels)
		diags.Append(d...)
		m.AllowLabels = val
	} else {
		m.AllowLabels = types.MapNull(types.ListType{ElemType: types.StringType})
	}
	if len(role.AllowNames) > 0 {
		val, d := types.ListValueFrom(ctx, types.StringType, role.AllowNames)
		diags.Append(d...)
		m.AllowNames = val
	} else {
		m.AllowNames = types.ListNull(types.StringType)
	}
	if len(role.DenyLabels) > 0 {
		val, d := types.MapValueFrom(ctx, types.ListType{ElemType: types.StringType}, role.DenyLabels)
		diags.Append(d...)
		m.DenyLabels = val
	} else {
		m.DenyLabels = types.MapNull(types.ListType{ElemType: types.StringType})
	}
	if len(role.DenyNames) > 0 {
		val, d := types.ListValueFrom(ctx, types.StringType, role.DenyNames)
		diags.Append(d...)
		m.DenyNames = val
	} else {
		m.DenyNames = types.ListNull(types.StringType)
	}

	return diags
}

// mapFromTFMap projects a Terraform Map into a Go map[string]string.
// Caller is responsible for guarding against IsNull/IsUnknown.
func mapFromTFMap(m types.Map) map[string][]string {
	out := map[string][]string{}
	for k, v := range m.Elements() {
		list, ok := v.(types.List)
		if !ok {
			continue
		}
		vals := make([]string, 0, len(list.Elements()))
		for _, e := range list.Elements() {
			vals = append(vals, e.(types.String).ValueString())
		}
		out[k] = vals
	}
	return out
}

// mapFromTFMapOrEmpty returns the projected map or an empty map when
// the Terraform Map is null/unknown. Used by Update where "no labels
// in HCL" means "clear labels on the server".
func mapFromTFMapOrEmpty(m types.Map) map[string][]string {
	if m.IsNull() || m.IsUnknown() {
		return map[string][]string{}
	}
	return mapFromTFMap(m)
}

func sliceFromTFList(l types.List) []string {
	out := make([]string, 0, len(l.Elements()))
	for _, v := range l.Elements() {
		out = append(out, v.(types.String).ValueString())
	}
	return out
}

func sliceFromTFListOrEmpty(l types.List) []string {
	if l.IsNull() || l.IsUnknown() {
		return []string{}
	}
	return sliceFromTFList(l)
}

// sliceFromTFSet projects a Terraform Set of strings into a Go slice.
// Caller is responsible for guarding against IsNull/IsUnknown.
func sliceFromTFSet(s types.Set) []string {
	out := make([]string, 0, len(s.Elements()))
	for _, v := range s.Elements() {
		out = append(out, v.(types.String).ValueString())
	}
	return out
}
