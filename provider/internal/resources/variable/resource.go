package variable

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

var (
	_ resource.Resource                = &variableResource{}
	_ resource.ResourceWithImportState = &variableResource{}
)

// variableResource — migrated to go-terrapod (#347). The legacy
// *client.Client field is kept for the cross-resource path in case
// other helpers in this package need it; CRUD goes entirely through
// the typed `tc` client. The legacy field disappears once all
// resources migrate.
type variableResource struct {
	client *client.Client
	tc     *terrapod.Client
}

func NewResource() resource.Resource {
	return &variableResource{}
}

func (r *variableResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_variable"
}

func (r *variableResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a workspace variable in Terrapod.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed: true, Description: "Variable ID.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"workspace_id": schema.StringAttribute{
				Required: true, Description: "Workspace ID this variable belongs to.",
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
			"created_at": schema.StringAttribute{
				Computed: true, Description: "Creation timestamp.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"updated_at": schema.StringAttribute{
				Computed: true, Description: "Update timestamp.",
			},
		},
	}
}

func (r *variableResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *variableResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan variableModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	v, err := r.tc.CreateVariable(ctx, plan.WorkspaceID.ValueString(), buildCreateVariableRequest(&plan))
	if err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}

	readVariableIntoModel(v, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *variableResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state variableModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	v, err := r.tc.GetVariable(ctx, state.WorkspaceID.ValueString(), state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}

	readVariableIntoModel(v, &state)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *variableResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan variableModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var state variableModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	v, err := r.tc.UpdateVariable(ctx,
		state.WorkspaceID.ValueString(),
		state.ID.ValueString(),
		buildUpdateVariableRequest(&plan),
	)
	if err != nil {
		resp.Diagnostics.AddError("Update failed", err.Error())
		return
	}

	readVariableIntoModel(v, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *variableResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state variableModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	err := r.tc.DeleteVariable(ctx, state.WorkspaceID.ValueString(), state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Delete failed", err.Error())
		}
	}
}

func (r *variableResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	// Import format: workspace_id/variable_id
	parts := strings.SplitN(req.ID, "/", 2)
	if len(parts) != 2 {
		resp.Diagnostics.AddError("Invalid import ID", "Expected format: workspace_id/variable_id")
		return
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("workspace_id"), parts[0])...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), parts[1])...)
}

// buildCreateVariableRequest projects the Terraform model into the
// SDK's typed CreateVariableRequest. Optional bool/string fields are
// included only when explicitly set, matching the previous
// map[string]any semantics.
func buildCreateVariableRequest(m *variableModel) terrapod.CreateVariableRequest {
	req := terrapod.CreateVariableRequest{
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

// buildUpdateVariableRequest is the partial-update shape. Pointer
// fields on the SDK request preserve "leave alone" semantics — nil ↦
// omit from body, &value ↦ set explicitly. Category is sent only on
// update (the schema makes it RequiresReplace, but if Terraform asks
// us to PATCH we honour that).
func buildUpdateVariableRequest(m *variableModel) terrapod.UpdateVariableRequest {
	req := terrapod.UpdateVariableRequest{
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

// readVariableIntoModel projects a SDK Variable into the Terraform
// model. Sensitive values come back redacted from the server — we
// don't overwrite the model's Value (which holds the operator's
// configured value); other fields are refreshed.
func readVariableIntoModel(v *terrapod.Variable, m *variableModel) {
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
	// Don't touch Value on a Sensitive read — server returns empty,
	// the model's existing value (from plan) is the source of truth.
	if !v.Sensitive && v.Value != "" {
		m.Value = types.StringValue(v.Value)
	}
}

// readIntoModel: removed; replaced by readVariableIntoModel above
// (which works on the typed go-terrapod Variable rather than the raw
// JSON:API Resource). The old function was the last consumer of the
// `client.Resource` / `client.GetXAttr` helpers from this file, so
// deleting it cleans up the import surface too.
