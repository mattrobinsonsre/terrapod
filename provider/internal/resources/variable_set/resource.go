// Package variable_set implements the terrapod_variable_set resource.
//
// API Contract (Terrapod API <-> Terraform Provider):
//
//	JSON:API type: "varsets"
//	ID prefix: "varset-"
//	Create:  POST   /api/v2/organizations/default/varsets
//	Read:    GET    /api/v2/varsets/{id}
//	Update:  PATCH  /api/v2/varsets/{id}
//	Delete:  DELETE /api/v2/varsets/{id}
//
// Attribute mapping (JSON:API -> Terraform):
//
//	"name"        -> name        (string, required)
//	"description" -> description (string, optional)
//	"global"      -> global      (bool, optional, default false)
//	"priority"    -> priority    (bool, optional, default false)
//
// Read-only:
//
//	"var-count"       -> var_count       (int, computed)
//	"workspace-count" -> workspace_count (int, computed)
//	"created-at"      -> created_at      (string, computed)
//	"updated-at"      -> updated_at      (string, computed)
//
// Import: by variable set ID.
package variable_set

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

// variableSetModel maps the Terraform schema to Go types.
type variableSetModel struct {
	ID types.String `tfsdk:"id"`

	// Writable attributes
	Name        types.String `tfsdk:"name"`
	Description types.String `tfsdk:"description"`
	Global      types.Bool   `tfsdk:"global"`
	Priority    types.Bool   `tfsdk:"priority"`

	// Read-only attributes
	VarCount       types.Int64  `tfsdk:"var_count"`
	WorkspaceCount types.Int64  `tfsdk:"workspace_count"`
	CreatedAt      types.String `tfsdk:"created_at"`
	UpdatedAt      types.String `tfsdk:"updated_at"`
}

var (
	_ resource.Resource                = &variableSetResource{}
	_ resource.ResourceWithImportState = &variableSetResource{}
)

type variableSetResource struct {
	client *client.Client
}

// NewResource returns a new variable set resource.
func NewResource() resource.Resource {
	return &variableSetResource{}
}

func (r *variableSetResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_variable_set"
}

func (r *variableSetResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a variable set in Terrapod.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed: true, Description: "Variable set ID (e.g. varset-abc123).",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"name": schema.StringAttribute{
				Required: true, Description: "The variable set name.",
			},
			"description": schema.StringAttribute{
				Optional: true, Description: "Description of the variable set.",
			},
			"global": schema.BoolAttribute{
				Optional: true, Computed: true, Default: booldefault.StaticBool(false),
				Description: "Apply this variable set to all workspaces.",
			},
			"priority": schema.BoolAttribute{
				Optional: true, Computed: true, Default: booldefault.StaticBool(false),
				Description: "Priority variable sets override workspace variables.",
			},
			"var_count": schema.Int64Attribute{
				Computed: true, Description: "Number of variables in this set.",
			},
			"workspace_count": schema.Int64Attribute{
				Computed: true, Description: "Number of workspaces assigned to this set.",
			},
			"created_at": schema.StringAttribute{Computed: true, Description: "Creation timestamp."},
			"updated_at": schema.StringAttribute{Computed: true, Description: "Update timestamp."},
		},
	}
}

func (r *variableSetResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *variableSetResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan variableSetModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs := buildAttrs(&plan)
	body, err := client.MarshalResource("varsets", attrs, nil)
	if err != nil {
		resp.Diagnostics.AddError("Marshal error", err.Error())
		return
	}

	data, err := r.client.Post(ctx, "/api/v2/organizations/default/varsets", body)
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

func (r *variableSetResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state variableSetModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	data, err := r.client.Get(ctx, "/api/v2/varsets/"+state.ID.ValueString())
	if err != nil {
		if client.IsNotFound(err) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Parse error", err.Error())
		return
	}

	readIntoModel(res, &state)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *variableSetResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan variableSetModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var state variableSetModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs := buildAttrs(&plan)
	body, err := client.MarshalResourceWithID(state.ID.ValueString(), "varsets", attrs)
	if err != nil {
		resp.Diagnostics.AddError("Marshal error", err.Error())
		return
	}

	data, err := r.client.Patch(ctx, "/api/v2/varsets/"+state.ID.ValueString(), body)
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

func (r *variableSetResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state variableSetModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	err := r.client.Delete(ctx, "/api/v2/varsets/"+state.ID.ValueString())
	if err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Delete failed", err.Error())
	}
}

func (r *variableSetResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

func buildAttrs(m *variableSetModel) map[string]any {
	attrs := map[string]any{
		"name": m.Name.ValueString(),
	}
	if !m.Description.IsNull() {
		attrs["description"] = m.Description.ValueString()
	}
	if !m.Global.IsNull() && !m.Global.IsUnknown() {
		attrs["global"] = m.Global.ValueBool()
	}
	if !m.Priority.IsNull() && !m.Priority.IsUnknown() {
		attrs["priority"] = m.Priority.ValueBool()
	}
	return attrs
}

func readIntoModel(res *client.Resource, m *variableSetModel) {
	m.ID = types.StringValue(res.ID)
	m.Name = types.StringValue(client.GetStringAttr(res, "name"))
	m.Global = types.BoolValue(client.GetBoolAttr(res, "global"))
	m.Priority = types.BoolValue(client.GetBoolAttr(res, "priority"))
	m.VarCount = types.Int64Value(client.GetIntAttr(res, "var-count"))
	m.WorkspaceCount = types.Int64Value(client.GetIntAttr(res, "workspace-count"))
	m.CreatedAt = types.StringValue(client.GetStringAttr(res, "created-at"))
	m.UpdatedAt = types.StringValue(client.GetStringAttr(res, "updated-at"))

	if v := client.GetStringAttr(res, "description"); v != "" {
		m.Description = types.StringValue(v)
	} else {
		m.Description = types.StringNull()
	}
}
