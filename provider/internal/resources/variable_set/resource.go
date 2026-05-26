// Package variable_set implements the terrapod_variable_set resource.
// Migrated to go-terrapod (#347).
package variable_set

import (
	"context"
	"errors"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

type variableSetModel struct {
	ID types.String `tfsdk:"id"`

	Name        types.String `tfsdk:"name"`
	Description types.String `tfsdk:"description"`
	Global      types.Bool   `tfsdk:"global"`
	Priority    types.Bool   `tfsdk:"priority"`

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
	tc     *terrapod.Client
}

func NewResource() resource.Resource { return &variableSetResource{} }

func (r *variableSetResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_variable_set"
}

func (r *variableSetResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a variable set in Terrapod.",
		Attributes: map[string]schema.Attribute{
			"id":          schema.StringAttribute{Computed: true, Description: "Variable set ID.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"name":        schema.StringAttribute{Required: true, Description: "The variable set name."},
			"description": schema.StringAttribute{Optional: true, Description: "Description of the variable set."},
			"global":      schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(false), Description: "Apply this variable set to all workspaces."},
			"priority":    schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(false), Description: "Priority variable sets override workspace variables."},
			"var_count":   schema.Int64Attribute{Computed: true, Description: "Number of variables in this set."},
			"workspace_count": schema.Int64Attribute{Computed: true, Description: "Number of workspaces assigned to this set."},
			"created_at":  schema.StringAttribute{Computed: true, Description: "Creation timestamp.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"updated_at":  schema.StringAttribute{Computed: true, Description: "Update timestamp."},
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
	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: c.BaseURL, Token: c.Token})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	r.tc = tc
}

func (r *variableSetResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan variableSetModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	v, err := r.tc.CreateVariableSet(ctx, buildCreateVarsetRequest(&plan))
	if err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}
	readVarsetFromSDK(v, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *variableSetResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state variableSetModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	v, err := r.tc.GetVariableSet(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}
	readVarsetFromSDK(v, &state)
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
	v, err := r.tc.UpdateVariableSet(ctx, state.ID.ValueString(), buildUpdateVarsetRequest(&plan))
	if err != nil {
		resp.Diagnostics.AddError("Update failed", err.Error())
		return
	}
	readVarsetFromSDK(v, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *variableSetResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state variableSetModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	err := r.tc.DeleteVariableSet(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Delete failed", err.Error())
		}
	}
}

func (r *variableSetResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

func buildCreateVarsetRequest(m *variableSetModel) terrapod.CreateVariableSetRequest {
	req := terrapod.CreateVariableSetRequest{
		Name: m.Name.ValueString(),
	}
	if !m.Description.IsNull() {
		req.Description = m.Description.ValueString()
	}
	if !m.Global.IsNull() && !m.Global.IsUnknown() {
		req.Global = m.Global.ValueBool()
	}
	if !m.Priority.IsNull() && !m.Priority.IsUnknown() {
		req.Priority = m.Priority.ValueBool()
	}
	return req
}

func buildUpdateVarsetRequest(m *variableSetModel) terrapod.UpdateVariableSetRequest {
	req := terrapod.UpdateVariableSetRequest{
		Name: m.Name.ValueString(),
	}
	if !m.Description.IsNull() && !m.Description.IsUnknown() {
		d := m.Description.ValueString()
		req.Description = &d
	}
	if !m.Global.IsNull() && !m.Global.IsUnknown() {
		g := m.Global.ValueBool()
		req.Global = &g
	}
	if !m.Priority.IsNull() && !m.Priority.IsUnknown() {
		p := m.Priority.ValueBool()
		req.Priority = &p
	}
	return req
}

func readVarsetFromSDK(v *terrapod.VariableSet, m *variableSetModel) {
	m.ID = types.StringValue(v.ID)
	m.Name = types.StringValue(v.Name)
	m.Global = types.BoolValue(v.Global)
	m.Priority = types.BoolValue(v.Priority)
	m.VarCount = types.Int64Value(v.VarCount)
	m.WorkspaceCount = types.Int64Value(v.WorkspaceCount)
	m.CreatedAt = types.StringValue(v.CreatedAt)
	m.UpdatedAt = types.StringValue(v.UpdatedAt)
	if v.Description != "" {
		m.Description = types.StringValue(v.Description)
	} else {
		m.Description = types.StringNull()
	}
}
