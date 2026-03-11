// Package variable_set_variable implements the terrapod_variable_set_variable resource.
//
// API Contract (Terrapod API <-> Terraform Provider):
//
//	JSON:API type: "vars"
//	ID prefix: "var-"
//	Create:  POST   /api/v2/varsets/{varset_id}/relationships/vars
//	Read:    GET    /api/v2/varsets/{varset_id}/relationships/vars  (list, filter by ID)
//	Update:  PATCH  /api/v2/varsets/{varset_id}/relationships/vars/{var_id}
//	Delete:  DELETE /api/v2/varsets/{varset_id}/relationships/vars/{var_id}
//
// Attribute mapping (JSON:API -> Terraform):
//
//	"key"         -> key         (string, required)
//	"value"       -> value       (string, optional, sensitive when sensitive=true)
//	"category"    -> category    (string, required: "terraform" or "env")
//	"hcl"         -> hcl         (bool, optional)
//	"sensitive"   -> sensitive   (bool, optional)
//	"description" -> description (string, optional)
//
// Read-only:
//
//	"version-id"  -> version_id  (string, computed)
//	"created-at"  -> created_at  (string, computed)
//	"updated-at"  -> updated_at  (string, computed)
//
// Note: When sensitive=true, the API returns value=null. The provider stores
// the configured value in state and never reads it back from the API.
//
// Import: varset_id/variable_id
package variable_set_variable

import (
	"context"
	"fmt"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

// variableSetVariableModel maps the Terraform schema to Go types.
type variableSetVariableModel struct {
	ID       types.String `tfsdk:"id"`
	VarsetID types.String `tfsdk:"varset_id"`

	// Writable attributes
	Key         types.String `tfsdk:"key"`
	Value       types.String `tfsdk:"value"`
	Category    types.String `tfsdk:"category"`
	HCL         types.Bool   `tfsdk:"hcl"`
	Sensitive   types.Bool   `tfsdk:"sensitive"`
	Description types.String `tfsdk:"description"`

	// Read-only attributes
	VersionID types.String `tfsdk:"version_id"`
	CreatedAt types.String `tfsdk:"created_at"`
	UpdatedAt types.String `tfsdk:"updated_at"`
}

var (
	_ resource.Resource                = &variableSetVariableResource{}
	_ resource.ResourceWithImportState = &variableSetVariableResource{}
)

type variableSetVariableResource struct {
	client *client.Client
}

// NewResource returns a new variable set variable resource.
func NewResource() resource.Resource {
	return &variableSetVariableResource{}
}

func (r *variableSetVariableResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_variable_set_variable"
}

func (r *variableSetVariableResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a variable within a Terrapod variable set.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed: true, Description: "Variable ID (e.g. var-abc123).",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"varset_id": schema.StringAttribute{
				Required: true, Description: "Variable set ID this variable belongs to.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"key": schema.StringAttribute{
				Required: true, Description: "Variable name.",
			},
			"value": schema.StringAttribute{
				Optional: true, Sensitive: true, Description: "Variable value. Sensitive variables are write-only.",
			},
			"category": schema.StringAttribute{
				Required: true, Description: "Category: terraform or env.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"hcl": schema.BoolAttribute{
				Optional: true, Computed: true, Default: booldefault.StaticBool(false),
				Description: "Parse value as HCL.",
			},
			"sensitive": schema.BoolAttribute{
				Optional: true, Computed: true, Default: booldefault.StaticBool(false),
				Description: "Mark as sensitive (value will not be returned by API).",
			},
			"description": schema.StringAttribute{
				Optional: true, Description: "Description.",
			},
			"version_id": schema.StringAttribute{
				Computed: true, Description: "Version identifier.",
			},
			"created_at": schema.StringAttribute{Computed: true, Description: "Creation timestamp."},
			"updated_at": schema.StringAttribute{Computed: true, Description: "Update timestamp."},
		},
	}
}

func (r *variableSetVariableResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *variableSetVariableResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan variableSetVariableModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs := buildAttrs(&plan)
	body, err := client.MarshalResource("vars", attrs, nil)
	if err != nil {
		resp.Diagnostics.AddError("Marshal error", err.Error())
		return
	}

	data, err := r.client.Post(ctx, fmt.Sprintf("/api/v2/varsets/%s/relationships/vars", plan.VarsetID.ValueString()), body)
	if err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Parse error", err.Error())
		return
	}

	readIntoModel(res, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *variableSetVariableResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state variableSetVariableModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	// List all variables for the variable set and find ours by ID.
	data, err := r.client.Get(ctx, fmt.Sprintf("/api/v2/varsets/%s/relationships/vars", state.VarsetID.ValueString()))
	if err != nil {
		if client.IsNotFound(err) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}

	resources, err := client.ParseResourceList(data)
	if err != nil {
		resp.Diagnostics.AddError("Parse error", err.Error())
		return
	}

	var found *client.Resource
	for i := range resources {
		if resources[i].ID == state.ID.ValueString() {
			found = &resources[i]
			break
		}
	}
	if found == nil {
		resp.State.RemoveResource(ctx)
		return
	}

	readIntoModel(found, &state)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *variableSetVariableResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan variableSetVariableModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var state variableSetVariableModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs := buildAttrs(&plan)
	body, err := client.MarshalResourceWithID(state.ID.ValueString(), "vars", attrs)
	if err != nil {
		resp.Diagnostics.AddError("Marshal error", err.Error())
		return
	}

	data, err := r.client.Patch(ctx, fmt.Sprintf("/api/v2/varsets/%s/relationships/vars/%s", state.VarsetID.ValueString(), state.ID.ValueString()), body)
	if err != nil {
		resp.Diagnostics.AddError("Update failed", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Parse error", err.Error())
		return
	}

	readIntoModel(res, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *variableSetVariableResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state variableSetVariableModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	err := r.client.Delete(ctx, fmt.Sprintf("/api/v2/varsets/%s/relationships/vars/%s", state.VarsetID.ValueString(), state.ID.ValueString()))
	if err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Delete failed", err.Error())
	}
}

func (r *variableSetVariableResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	// Import format: varset_id/variable_id
	parts := strings.SplitN(req.ID, "/", 2)
	if len(parts) != 2 {
		resp.Diagnostics.AddError("Invalid import ID", "Expected format: varset_id/variable_id")
		return
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("varset_id"), parts[0])...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), parts[1])...)
}

func buildAttrs(m *variableSetVariableModel) map[string]any {
	attrs := map[string]any{
		"key":      m.Key.ValueString(),
		"category": m.Category.ValueString(),
	}
	if !m.Value.IsNull() {
		attrs["value"] = m.Value.ValueString()
	}
	if !m.HCL.IsNull() && !m.HCL.IsUnknown() {
		attrs["hcl"] = m.HCL.ValueBool()
	}
	if !m.Sensitive.IsNull() && !m.Sensitive.IsUnknown() {
		attrs["sensitive"] = m.Sensitive.ValueBool()
	}
	if !m.Description.IsNull() {
		attrs["description"] = m.Description.ValueString()
	}
	return attrs
}

func readIntoModel(res *client.Resource, m *variableSetVariableModel) {
	m.ID = types.StringValue(res.ID)
	m.Key = types.StringValue(client.GetStringAttr(res, "key"))
	m.Category = types.StringValue(client.GetStringAttr(res, "category"))
	m.HCL = types.BoolValue(client.GetBoolAttr(res, "hcl"))
	m.Sensitive = types.BoolValue(client.GetBoolAttr(res, "sensitive"))
	m.VersionID = types.StringValue(client.GetStringAttr(res, "version-id"))
	m.CreatedAt = types.StringValue(client.GetStringAttr(res, "created-at"))
	m.UpdatedAt = types.StringValue(client.GetStringAttr(res, "updated-at"))

	if v := client.GetStringAttr(res, "description"); v != "" {
		m.Description = types.StringValue(v)
	} else {
		m.Description = types.StringNull()
	}

	// Sensitive variables: API returns null. Preserve configured value from state/plan.
	if m.Sensitive.ValueBool() {
		// Value stays as-is from plan (not overwritten by API null).
	} else {
		if v := client.GetStringAttr(res, "value"); v != "" {
			m.Value = types.StringValue(v)
		} else {
			m.Value = types.StringNull()
		}
	}
}
