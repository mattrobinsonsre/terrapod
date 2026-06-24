// Package role implements the terrapod_role resource.
//
// API Contract (Terrapod API ↔ Terraform Provider):
//
//	JSON:API type: "roles"
//	ID: role name (string, not UUID)
//	Create:  POST   /api/terrapod/v1/roles
//	Read:    GET    /api/terrapod/v1/roles/{name}
//	Update:  PATCH  /api/terrapod/v1/roles/{name}
//	Delete:  DELETE /api/terrapod/v1/roles/{name}
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
)

var (
	_ resource.Resource                = &roleResource{}
	_ resource.ResourceWithImportState = &roleResource{}
)

type roleModel struct {
	ID types.String `tfsdk:"id"`

	Name                types.String `tfsdk:"name"`
	Description         types.String `tfsdk:"description"`
	AllowLabels         types.Map    `tfsdk:"allow_labels"`
	AllowNames          types.List   `tfsdk:"allow_names"`
	DenyLabels          types.Map    `tfsdk:"deny_labels"`
	DenyNames           types.List   `tfsdk:"deny_names"`
	WorkspacePermission types.String `tfsdk:"workspace_permission"`
	PoolPermission      types.String `tfsdk:"pool_permission"`
	RegistryPermission  types.String `tfsdk:"registry_permission"`
	CatalogPermission   types.String `tfsdk:"catalog_permission"`

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
	resp.Schema = schema.Schema{
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
				Optional:    true,
			},
			"allow_labels": schema.MapAttribute{
				Description: "Labels that grant this role's permission to matching workspaces.",
				Optional:    true,
				ElementType: types.StringType,
			},
			"allow_names": schema.ListAttribute{
				Description: "Workspace names that this role grants access to.",
				Optional:    true,
				ElementType: types.StringType,
			},
			"deny_labels": schema.MapAttribute{
				Description: "Labels that deny this role's permission from matching workspaces.",
				Optional:    true,
				ElementType: types.StringType,
			},
			"deny_names": schema.ListAttribute{
				Description: "Workspace names that this role denies access to.",
				Optional:    true,
				ElementType: types.StringType,
			},
			"workspace_permission": schema.StringAttribute{
				Description: "Workspace permission level: read, plan, write, or admin.",
				Required:    true,
			},
			"pool_permission": schema.StringAttribute{
				Description: "Agent pool permission level: read, write, or admin. Defaults to read.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"registry_permission": schema.StringAttribute{
				Description: "Registry permission level for modules and providers: read, write, or admin. Defaults to read.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"catalog_permission": schema.StringAttribute{
				Description: "Service-catalog permission level: none, read, use, or admin. Opt-in (no everyone floor); defaults to none.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
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

	role, err := r.tc.UpdateRole(ctx, state.ID.ValueString(), buildUpdateRoleRequest(&plan))
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
	if !m.Description.IsNull() {
		req.Description = m.Description.ValueString()
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
func buildUpdateRoleRequest(m *roleModel) terrapod.UpdateRoleRequest {
	req := terrapod.UpdateRoleRequest{
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
	if !m.Description.IsNull() && !m.Description.IsUnknown() {
		d := m.Description.ValueString()
		req.Description = &d
	}
	// Allow/deny — null on the model ⇒ empty pointer (clear); set ⇒
	// pointer to the materialised value. This matches the old
	// behaviour where the resource always wrote allow/deny to the API
	// (omitting allow_labels in HCL cleared them on the server).
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

	m.BuiltIn = types.BoolValue(role.BuiltIn)
	m.CreatedAt = types.StringValue(role.CreatedAt)
	m.UpdatedAt = types.StringValue(role.UpdatedAt)

	if role.Description != "" {
		m.Description = types.StringValue(role.Description)
	} else {
		m.Description = types.StringNull()
	}

	if len(role.AllowLabels) > 0 {
		val, d := types.MapValueFrom(ctx, types.StringType, role.AllowLabels)
		diags.Append(d...)
		m.AllowLabels = val
	} else {
		m.AllowLabels = types.MapNull(types.StringType)
	}
	if len(role.AllowNames) > 0 {
		val, d := types.ListValueFrom(ctx, types.StringType, role.AllowNames)
		diags.Append(d...)
		m.AllowNames = val
	} else {
		m.AllowNames = types.ListNull(types.StringType)
	}
	if len(role.DenyLabels) > 0 {
		val, d := types.MapValueFrom(ctx, types.StringType, role.DenyLabels)
		diags.Append(d...)
		m.DenyLabels = val
	} else {
		m.DenyLabels = types.MapNull(types.StringType)
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
func mapFromTFMap(m types.Map) map[string]string {
	out := map[string]string{}
	for k, v := range m.Elements() {
		out[k] = v.(types.String).ValueString()
	}
	return out
}

// mapFromTFMapOrEmpty returns the projected map or an empty map when
// the Terraform Map is null/unknown. Used by Update where "no labels
// in HCL" means "clear labels on the server".
func mapFromTFMapOrEmpty(m types.Map) map[string]string {
	if m.IsNull() || m.IsUnknown() {
		return map[string]string{}
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
