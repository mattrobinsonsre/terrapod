// Package role implements the terrapod_role resource.
//
// API Contract (Terrapod API ↔ Terraform Provider):
//
//	JSON:API type: "roles"
//	ID: role name (string, not UUID)
//	Create:  POST   /api/v2/roles
//	Read:    GET    /api/v2/roles/{name}
//	Update:  PATCH  /api/v2/roles/{name}
//	Delete:  DELETE /api/v2/roles/{name}
//	List:    GET    /api/v2/roles
//
// The API uses the role name as the resource identifier — there is no
// UUID or prefixed ID. The "name" field in the JSON:API response sits
// at the top level alongside "type", not inside "attributes".
//
// Attribute mapping (JSON:API attribute → Terraform schema attribute):
//
//	(top-level "name")              → name                 (string, required, forces new — used as ID)
//	"description"                   → description          (string, optional)
//	"allow-labels"                  → allow_labels         (map[string]string, optional)
//	"allow-names"                   → allow_names          (list of strings, optional)
//	"deny-labels"                   → deny_labels          (map[string]string, optional)
//	"deny-names"                    → deny_names           (list of strings, optional)
//	"workspace-permission"          → workspace_permission (string, required: read/plan/write/admin)
//	"pool-permission"               → pool_permission      (string, optional: read/write/admin, default "read")
//
// Read-only attributes:
//
//	"built-in"   → built_in    (bool,   computed)
//	"created-at" → created_at  (string, computed)
//	"updated-at" → updated_at  (string, computed)
//
// Import: by role name.
package role

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/boolplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var (
	_ resource.Resource                = &roleResource{}
	_ resource.ResourceWithImportState = &roleResource{}
)

// roleModel maps the Terraform schema to Go types.
type roleModel struct {
	ID types.String `tfsdk:"id"`

	// Writable attributes
	Name                types.String `tfsdk:"name"`
	Description         types.String `tfsdk:"description"`
	AllowLabels         types.Map    `tfsdk:"allow_labels"`
	AllowNames          types.List   `tfsdk:"allow_names"`
	DenyLabels          types.Map    `tfsdk:"deny_labels"`
	DenyNames           types.List   `tfsdk:"deny_names"`
	WorkspacePermission types.String `tfsdk:"workspace_permission"`
	PoolPermission      types.String `tfsdk:"pool_permission"`

	// Read-only attributes
	BuiltIn   types.Bool   `tfsdk:"built_in"`
	CreatedAt types.String `tfsdk:"created_at"`
	UpdatedAt types.String `tfsdk:"updated_at"`
}

type roleResource struct {
	client *client.Client
}

// NewResource returns a new role resource.
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

			// Read-only
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
}

func (r *roleResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan roleModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs := buildRoleAttrs(&plan)

	// The roles API expects "name" at the data level, not inside attributes.
	body, err := marshalRoleRequest(plan.Name.ValueString(), attrs)
	if err != nil {
		resp.Diagnostics.AddError("Marshal error", err.Error())
		return
	}

	data, err := r.client.Post(ctx, "/api/v2/roles", body)
	if err != nil {
		resp.Diagnostics.AddError("Failed to create role", err.Error())
		return
	}

	res, err := parseRoleResponse(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	resp.Diagnostics.Append(readRoleIntoModel(ctx, res, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *roleResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state roleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	data, err := r.client.Get(ctx, "/api/v2/roles/"+state.ID.ValueString())
	if err != nil {
		if client.IsNotFound(err) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Failed to read role", err.Error())
		return
	}

	res, err := parseRoleResponse(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	resp.Diagnostics.Append(readRoleIntoModel(ctx, res, &state)...)
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

	attrs := buildRoleAttrs(&plan)
	body, err := marshalRoleRequest(state.ID.ValueString(), attrs)
	if err != nil {
		resp.Diagnostics.AddError("Marshal error", err.Error())
		return
	}

	data, err := r.client.Patch(ctx, "/api/v2/roles/"+state.ID.ValueString(), body)
	if err != nil {
		resp.Diagnostics.AddError("Failed to update role", err.Error())
		return
	}

	res, err := parseRoleResponse(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	resp.Diagnostics.Append(readRoleIntoModel(ctx, res, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *roleResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state roleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	err := r.client.Delete(ctx, "/api/v2/roles/"+state.ID.ValueString())
	if err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Failed to delete role", err.Error())
	}
}

func (r *roleResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	// Import by role name — the name IS the ID.
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), req.ID)...)
}

// buildRoleAttrs converts the Terraform model into JSON:API attributes.
func buildRoleAttrs(m *roleModel) map[string]any {
	attrs := map[string]any{
		"workspace-permission": m.WorkspacePermission.ValueString(),
	}

	if !m.PoolPermission.IsNull() && !m.PoolPermission.IsUnknown() {
		attrs["pool-permission"] = m.PoolPermission.ValueString()
	}

	if !m.Description.IsNull() {
		attrs["description"] = m.Description.ValueString()
	}

	if !m.AllowLabels.IsNull() && !m.AllowLabels.IsUnknown() {
		labels := map[string]string{}
		for k, v := range m.AllowLabels.Elements() {
			labels[k] = v.(types.String).ValueString()
		}
		attrs["allow-labels"] = labels
	} else {
		attrs["allow-labels"] = map[string]string{}
	}

	if !m.AllowNames.IsNull() && !m.AllowNames.IsUnknown() {
		names := make([]string, 0, len(m.AllowNames.Elements()))
		for _, v := range m.AllowNames.Elements() {
			names = append(names, v.(types.String).ValueString())
		}
		attrs["allow-names"] = names
	} else {
		attrs["allow-names"] = []string{}
	}

	if !m.DenyLabels.IsNull() && !m.DenyLabels.IsUnknown() {
		labels := map[string]string{}
		for k, v := range m.DenyLabels.Elements() {
			labels[k] = v.(types.String).ValueString()
		}
		attrs["deny-labels"] = labels
	} else {
		attrs["deny-labels"] = map[string]string{}
	}

	if !m.DenyNames.IsNull() && !m.DenyNames.IsUnknown() {
		names := make([]string, 0, len(m.DenyNames.Elements()))
		for _, v := range m.DenyNames.Elements() {
			names = append(names, v.(types.String).ValueString())
		}
		attrs["deny-names"] = names
	} else {
		attrs["deny-names"] = []string{}
	}

	return attrs
}

// marshalRoleRequest builds the JSON body for role create/update.
// The roles API expects "name" at the data level (not "id").
func marshalRoleRequest(name string, attributes map[string]any) ([]byte, error) {
	body := map[string]any{
		"data": map[string]any{
			"name":       name,
			"type":       "roles",
			"attributes": attributes,
		},
	}
	return json.Marshal(body)
}

// roleResponseData holds the parsed role response. The roles API returns
// "name" at the top level of the data object instead of "id".
type roleResponseData struct {
	Name       string
	Attributes map[string]any
}

// parseRoleResponse extracts a role from a JSON:API response.
// The roles API uses "name" as the identifier, not "id".
func parseRoleResponse(data []byte) (*roleResponseData, error) {
	var doc struct {
		Data struct {
			Name       string         `json:"name"`
			Type       string         `json:"type"`
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, err
	}
	return &roleResponseData{
		Name:       doc.Data.Name,
		Attributes: doc.Data.Attributes,
	}, nil
}

// readRoleIntoModel populates the Terraform model from a parsed role response.
func readRoleIntoModel(ctx context.Context, res *roleResponseData, m *roleModel) diag.Diagnostics {
	var diags diag.Diagnostics

	m.ID = types.StringValue(res.Name)
	m.Name = types.StringValue(res.Name)

	m.WorkspacePermission = types.StringValue(getStringFromMap(res.Attributes, "workspace-permission"))
	if v := getStringFromMap(res.Attributes, "pool-permission"); v != "" {
		m.PoolPermission = types.StringValue(v)
	} else {
		m.PoolPermission = types.StringValue("read")
	}
	m.BuiltIn = types.BoolValue(getBoolFromMap(res.Attributes, "built-in"))
	m.CreatedAt = types.StringValue(getStringFromMap(res.Attributes, "created-at"))
	m.UpdatedAt = types.StringValue(getStringFromMap(res.Attributes, "updated-at"))

	if v := getStringFromMap(res.Attributes, "description"); v != "" {
		m.Description = types.StringValue(v)
	} else {
		m.Description = types.StringNull()
	}

	// Allow labels
	if raw, ok := res.Attributes["allow-labels"]; ok && raw != nil {
		if labels, ok := raw.(map[string]any); ok && len(labels) > 0 {
			strLabels := make(map[string]string, len(labels))
			for k, v := range labels {
				strLabels[k] = fmt.Sprintf("%v", v)
			}
			val, d := types.MapValueFrom(ctx, types.StringType, strLabels)
			diags.Append(d...)
			m.AllowLabels = val
		} else {
			m.AllowLabels = types.MapNull(types.StringType)
		}
	} else {
		m.AllowLabels = types.MapNull(types.StringType)
	}

	// Allow names
	if raw, ok := res.Attributes["allow-names"]; ok && raw != nil {
		if names, ok := raw.([]any); ok && len(names) > 0 {
			strNames := make([]string, 0, len(names))
			for _, v := range names {
				strNames = append(strNames, fmt.Sprintf("%v", v))
			}
			val, d := types.ListValueFrom(ctx, types.StringType, strNames)
			diags.Append(d...)
			m.AllowNames = val
		} else {
			m.AllowNames = types.ListNull(types.StringType)
		}
	} else {
		m.AllowNames = types.ListNull(types.StringType)
	}

	// Deny labels
	if raw, ok := res.Attributes["deny-labels"]; ok && raw != nil {
		if labels, ok := raw.(map[string]any); ok && len(labels) > 0 {
			strLabels := make(map[string]string, len(labels))
			for k, v := range labels {
				strLabels[k] = fmt.Sprintf("%v", v)
			}
			val, d := types.MapValueFrom(ctx, types.StringType, strLabels)
			diags.Append(d...)
			m.DenyLabels = val
		} else {
			m.DenyLabels = types.MapNull(types.StringType)
		}
	} else {
		m.DenyLabels = types.MapNull(types.StringType)
	}

	// Deny names
	if raw, ok := res.Attributes["deny-names"]; ok && raw != nil {
		if names, ok := raw.([]any); ok && len(names) > 0 {
			strNames := make([]string, 0, len(names))
			for _, v := range names {
				strNames = append(strNames, fmt.Sprintf("%v", v))
			}
			val, d := types.ListValueFrom(ctx, types.StringType, strNames)
			diags.Append(d...)
			m.DenyNames = val
		} else {
			m.DenyNames = types.ListNull(types.StringType)
		}
	} else {
		m.DenyNames = types.ListNull(types.StringType)
	}

	return diags
}

// getStringFromMap reads a string value from a map[string]any.
func getStringFromMap(m map[string]any, key string) string {
	v, ok := m[key]
	if !ok || v == nil {
		return ""
	}
	s, ok := v.(string)
	if !ok {
		return fmt.Sprintf("%v", v)
	}
	return s
}

// getBoolFromMap reads a bool value from a map[string]any.
func getBoolFromMap(m map[string]any, key string) bool {
	v, ok := m[key]
	if !ok || v == nil {
		return false
	}
	b, ok := v.(bool)
	if !ok {
		return false
	}
	return b
}
