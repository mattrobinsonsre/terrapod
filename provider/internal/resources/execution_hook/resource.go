// Package execution_hook implements the terrapod_execution_hook resource (#619).
package execution_hook

import (
	"context"
	"errors"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64default"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

type executionHookModel struct {
	ID types.String `tfsdk:"id"`

	Name        types.String `tfsdk:"name"`
	Description types.String `tfsdk:"description"`
	HookPoint   types.String `tfsdk:"hook_point"`
	Script      types.String `tfsdk:"script"`
	Enabled     types.Bool   `tfsdk:"enabled"`
	Priority    types.Int64  `tfsdk:"priority"`

	WorkspaceCount types.Int64  `tfsdk:"workspace_count"`
	CreatedAt      types.String `tfsdk:"created_at"`
	UpdatedAt      types.String `tfsdk:"updated_at"`
}

var (
	_ resource.Resource                = &executionHookResource{}
	_ resource.ResourceWithImportState = &executionHookResource{}
)

type executionHookResource struct {
	client *client.Client
	tc     *terrapod.Client
}

func NewResource() resource.Resource { return &executionHookResource{} }

func (r *executionHookResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_execution_hook"
}

func (r *executionHookResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a Terrapod execution hook: a custom shell step run inside the runner Job at a fixed point. Associate it with workspaces via terrapod_execution_hook_workspace. Requires platform admin.",
		Attributes: map[string]schema.Attribute{
			"id":          schema.StringAttribute{Computed: true, Description: "Execution hook ID.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"name":        schema.StringAttribute{Required: true, Description: "The hook name (unique)."},
			"description": schema.StringAttribute{Optional: true, Description: "Description of the hook."},
			"hook_point": schema.StringAttribute{
				Required:    true,
				Description: "When the hook runs: one of pre_init, pre_plan, post_plan, pre_apply, post_apply.",
			},
			"script": schema.StringAttribute{
				Optional:    true,
				Computed:    true,
				Default:     stringdefault.StaticString(""),
				Description: "Shell body run via /bin/sh -c inside the runner Job. Runs with the runner's cloud identity; a non-zero exit fails the run. Secrets should come from workspace variables, not inline here.",
			},
			"enabled": schema.BoolAttribute{
				Optional:    true,
				Computed:    true,
				Default:     booldefault.StaticBool(true),
				Description: "Whether the hook is active. Disabled hooks are not delivered to runs.",
			},
			"priority": schema.Int64Attribute{
				Optional:    true,
				Computed:    true,
				Default:     int64default.StaticInt64(0),
				Description: "Order among hooks sharing a point (lower runs first; ties broken by name).",
			},
			"workspace_count": schema.Int64Attribute{Computed: true, Description: "Number of workspaces associated with this hook."},
			"created_at":      schema.StringAttribute{Computed: true, Description: "Creation timestamp.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"updated_at":      schema.StringAttribute{Computed: true, Description: "Update timestamp."},
		},
	}
}

func (r *executionHookResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *executionHookResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan executionHookModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	h, err := r.tc.CreateExecutionHook(ctx, buildCreateHookRequest(&plan))
	if err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}
	readHookFromSDK(h, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *executionHookResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state executionHookModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	h, err := r.tc.GetExecutionHook(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}
	readHookFromSDK(h, &state)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *executionHookResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan executionHookModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var state executionHookModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	h, err := r.tc.UpdateExecutionHook(ctx, state.ID.ValueString(), buildUpdateHookRequest(&plan))
	if err != nil {
		resp.Diagnostics.AddError("Update failed", err.Error())
		return
	}
	readHookFromSDK(h, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *executionHookResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state executionHookModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	err := r.tc.DeleteExecutionHook(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Delete failed", err.Error())
		}
	}
}

func (r *executionHookResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

func buildCreateHookRequest(m *executionHookModel) terrapod.CreateExecutionHookRequest {
	req := terrapod.CreateExecutionHookRequest{
		Name:      m.Name.ValueString(),
		HookPoint: m.HookPoint.ValueString(),
		Script:    m.Script.ValueString(),
		Enabled:   m.Enabled.ValueBool(),
		Priority:  m.Priority.ValueInt64(),
	}
	if !m.Description.IsNull() {
		req.Description = m.Description.ValueString()
	}
	return req
}

func buildUpdateHookRequest(m *executionHookModel) terrapod.UpdateExecutionHookRequest {
	req := terrapod.UpdateExecutionHookRequest{
		Name: m.Name.ValueString(),
	}
	if !m.Description.IsNull() && !m.Description.IsUnknown() {
		d := m.Description.ValueString()
		req.Description = &d
	}
	if !m.HookPoint.IsNull() && !m.HookPoint.IsUnknown() {
		p := m.HookPoint.ValueString()
		req.HookPoint = &p
	}
	if !m.Script.IsNull() && !m.Script.IsUnknown() {
		s := m.Script.ValueString()
		req.Script = &s
	}
	if !m.Enabled.IsNull() && !m.Enabled.IsUnknown() {
		e := m.Enabled.ValueBool()
		req.Enabled = &e
	}
	if !m.Priority.IsNull() && !m.Priority.IsUnknown() {
		p := m.Priority.ValueInt64()
		req.Priority = &p
	}
	return req
}

func readHookFromSDK(h *terrapod.ExecutionHook, m *executionHookModel) {
	m.ID = types.StringValue(h.ID)
	m.Name = types.StringValue(h.Name)
	m.HookPoint = types.StringValue(h.HookPoint)
	m.Script = types.StringValue(h.Script)
	m.Enabled = types.BoolValue(h.Enabled)
	m.Priority = types.Int64Value(h.Priority)
	m.WorkspaceCount = types.Int64Value(h.WorkspaceCount)
	m.CreatedAt = types.StringValue(h.CreatedAt)
	m.UpdatedAt = types.StringValue(h.UpdatedAt)
	if h.Description != "" {
		m.Description = types.StringValue(h.Description)
	} else {
		m.Description = types.StringNull()
	}
}
