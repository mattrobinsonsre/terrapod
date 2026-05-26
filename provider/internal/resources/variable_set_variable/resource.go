// Package variable_set_variable implements the terrapod_variable_set_variable resource.
// Migrated to go-terrapod (#347).
package variable_set_variable

import (
	"context"
	"errors"
	"fmt"
	"strings"

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

type variableSetVariableModel struct {
	ID       types.String `tfsdk:"id"`
	VarsetID types.String `tfsdk:"varset_id"`

	Key         types.String `tfsdk:"key"`
	Value       types.String `tfsdk:"value"`
	Category    types.String `tfsdk:"category"`
	HCL         types.Bool   `tfsdk:"hcl"`
	Sensitive   types.Bool   `tfsdk:"sensitive"`
	Description types.String `tfsdk:"description"`

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
	tc     *terrapod.Client
}

func NewResource() resource.Resource { return &variableSetVariableResource{} }

func (r *variableSetVariableResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_variable_set_variable"
}

func (r *variableSetVariableResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a variable within a Terrapod variable set.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{Computed: true, Description: "Variable ID.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"varset_id": schema.StringAttribute{Required: true, Description: "Variable set ID this variable belongs to.", PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()}},
			"key":         schema.StringAttribute{Required: true, Description: "Variable name."},
			"value":       schema.StringAttribute{Optional: true, Sensitive: true, Description: "Variable value. Sensitive variables are write-only."},
			"category":    schema.StringAttribute{Required: true, Description: "Category: terraform or env.", PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()}},
			"hcl":         schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(false), Description: "Parse value as HCL."},
			"sensitive":   schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(false), Description: "Mark as sensitive (value will not be returned by API)."},
			"description": schema.StringAttribute{Optional: true, Description: "Description."},
			"version_id":  schema.StringAttribute{Computed: true, Description: "Version identifier."},
			"created_at":  schema.StringAttribute{Computed: true, Description: "Creation timestamp.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"updated_at":  schema.StringAttribute{Computed: true, Description: "Update timestamp."},
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
	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: c.BaseURL, Token: c.Token})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	r.tc = tc
}

func (r *variableSetVariableResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan variableSetVariableModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	v, err := r.tc.CreateVarsetVariable(ctx, plan.VarsetID.ValueString(), buildCreateVSVRequest(&plan))
	if err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}
	readVSVFromSDK(v, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *variableSetVariableResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state variableSetVariableModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	v, err := r.tc.GetVarsetVariable(ctx, state.VarsetID.ValueString(), state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}
	if v == nil {
		resp.State.RemoveResource(ctx)
		return
	}
	readVSVFromSDK(v, &state)
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
	v, err := r.tc.UpdateVarsetVariable(ctx, state.VarsetID.ValueString(), state.ID.ValueString(), buildUpdateVSVRequest(&plan))
	if err != nil {
		resp.Diagnostics.AddError("Update failed", err.Error())
		return
	}
	readVSVFromSDK(v, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *variableSetVariableResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state variableSetVariableModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	err := r.tc.DeleteVarsetVariable(ctx, state.VarsetID.ValueString(), state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Delete failed", err.Error())
		}
	}
}

func (r *variableSetVariableResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	parts := strings.SplitN(req.ID, "/", 2)
	if len(parts) != 2 {
		resp.Diagnostics.AddError("Invalid import ID", "Expected format: varset_id/variable_id")
		return
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("varset_id"), parts[0])...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), parts[1])...)
}

func buildCreateVSVRequest(m *variableSetVariableModel) terrapod.CreateVarsetVariableRequest {
	req := terrapod.CreateVarsetVariableRequest{
		Key:      m.Key.ValueString(),
		Category: m.Category.ValueString(),
	}
	if !m.Value.IsNull() {
		req.Value = m.Value.ValueString()
	}
	if !m.HCL.IsNull() && !m.HCL.IsUnknown() {
		req.HCL = m.HCL.ValueBool()
	}
	if !m.Sensitive.IsNull() && !m.Sensitive.IsUnknown() {
		req.Sensitive = m.Sensitive.ValueBool()
	}
	if !m.Description.IsNull() {
		req.Description = m.Description.ValueString()
	}
	return req
}

func buildUpdateVSVRequest(m *variableSetVariableModel) terrapod.UpdateVarsetVariableRequest {
	req := terrapod.UpdateVarsetVariableRequest{
		Key:      m.Key.ValueString(),
		Category: m.Category.ValueString(),
	}
	if !m.Value.IsNull() {
		v := m.Value.ValueString()
		req.Value = &v
	}
	if !m.HCL.IsNull() && !m.HCL.IsUnknown() {
		v := m.HCL.ValueBool()
		req.HCL = &v
	}
	if !m.Sensitive.IsNull() && !m.Sensitive.IsUnknown() {
		v := m.Sensitive.ValueBool()
		req.Sensitive = &v
	}
	if !m.Description.IsNull() {
		v := m.Description.ValueString()
		req.Description = &v
	}
	return req
}

func readVSVFromSDK(v *terrapod.VariableSetVariable, m *variableSetVariableModel) {
	m.ID = types.StringValue(v.ID)
	m.Key = types.StringValue(v.Key)
	m.Category = types.StringValue(v.Category)
	m.HCL = types.BoolValue(v.HCL)
	m.Sensitive = types.BoolValue(v.Sensitive)
	m.VersionID = types.StringValue(v.VersionID)
	m.CreatedAt = types.StringValue(v.CreatedAt)
	m.UpdatedAt = types.StringValue(v.UpdatedAt)
	if v.Description != "" {
		m.Description = types.StringValue(v.Description)
	} else {
		m.Description = types.StringNull()
	}
	if !v.Sensitive && v.Value != "" {
		m.Value = types.StringValue(v.Value)
	}
}
