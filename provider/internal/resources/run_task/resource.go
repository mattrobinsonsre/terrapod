// Package run_task — migrated to go-terrapod (#347).
package run_task

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

var (
	_ resource.Resource                = &runTaskResource{}
	_ resource.ResourceWithImportState = &runTaskResource{}
)

type runTaskResource struct {
	client *client.Client
	tc     *terrapod.Client
}

func NewResource() resource.Resource { return &runTaskResource{} }

func (r *runTaskResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_run_task"
}

func (r *runTaskResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a run task (pre/post-plan or pre-apply webhook) for a Terrapod workspace.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{Computed: true, Description: "Run task ID.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"workspace_id": schema.StringAttribute{Required: true, Description: "Workspace ID.", PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()}},
			"name":             schema.StringAttribute{Required: true, Description: "Task name."},
			"url":              schema.StringAttribute{Required: true, Description: "Webhook URL."},
			"enabled":          schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(true), Description: "Whether the task is enabled."},
			"stage":            schema.StringAttribute{Required: true, Description: "Stage: pre_plan, post_plan, or pre_apply."},
			"enforcement_level": schema.StringAttribute{Required: true, Description: "Enforcement: mandatory or advisory."},
			"hmac_key":         schema.StringAttribute{Optional: true, Sensitive: true, Description: "HMAC signing key (write-only)."},
			"has_hmac_key":     schema.BoolAttribute{Computed: true, Description: "Whether an HMAC key is configured."},
			"created_at":       schema.StringAttribute{Computed: true, Description: "Creation timestamp.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"updated_at":       schema.StringAttribute{Computed: true, Description: "Update timestamp."},
		},
	}
}

func (r *runTaskResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *runTaskResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan runTaskModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	rt, err := r.tc.CreateRunTask(ctx, plan.WorkspaceID.ValueString(), buildCreateRunTaskRequest(&plan))
	if err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}
	readRunTaskFromSDK(rt, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *runTaskResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state runTaskModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	rt, err := r.tc.GetRunTask(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}
	readRunTaskFromSDK(rt, &state)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *runTaskResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan runTaskModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var state runTaskModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	rt, err := r.tc.UpdateRunTask(ctx, state.ID.ValueString(), buildUpdateRunTaskRequest(&plan))
	if err != nil {
		resp.Diagnostics.AddError("Update failed", err.Error())
		return
	}
	readRunTaskFromSDK(rt, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *runTaskResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state runTaskModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	err := r.tc.DeleteRunTask(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Delete failed", err.Error())
		}
	}
}

func (r *runTaskResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

func buildCreateRunTaskRequest(m *runTaskModel) terrapod.CreateRunTaskRequest {
	req := terrapod.CreateRunTaskRequest{
		Name:             m.Name.ValueString(),
		URL:              m.URL.ValueString(),
		Stage:            m.Stage.ValueString(),
		EnforcementLevel: m.EnforcementLevel.ValueString(),
	}
	if !m.Enabled.IsNull() && !m.Enabled.IsUnknown() {
		v := m.Enabled.ValueBool()
		req.Enabled = &v
	}
	if !m.HMACKey.IsNull() {
		req.HMACKey = m.HMACKey.ValueString()
	}
	return req
}

func buildUpdateRunTaskRequest(m *runTaskModel) terrapod.UpdateRunTaskRequest {
	req := terrapod.UpdateRunTaskRequest{
		Name:             m.Name.ValueString(),
		URL:              m.URL.ValueString(),
		Stage:            m.Stage.ValueString(),
		EnforcementLevel: m.EnforcementLevel.ValueString(),
	}
	if !m.Enabled.IsNull() && !m.Enabled.IsUnknown() {
		v := m.Enabled.ValueBool()
		req.Enabled = &v
	}
	if !m.HMACKey.IsNull() && !m.HMACKey.IsUnknown() {
		req.HMACKey = m.HMACKey.ValueString()
	}
	return req
}

func readRunTaskFromSDK(rt *terrapod.RunTask, m *runTaskModel) {
	m.ID = types.StringValue(rt.ID)
	m.Name = types.StringValue(rt.Name)
	m.URL = types.StringValue(rt.URL)
	m.Enabled = types.BoolValue(rt.Enabled)
	m.Stage = types.StringValue(rt.Stage)
	m.EnforcementLevel = types.StringValue(rt.EnforcementLevel)
	m.HasHMACKey = types.BoolValue(rt.HasHMACKey)
	m.CreatedAt = types.StringValue(rt.CreatedAt)
	m.UpdatedAt = types.StringValue(rt.UpdatedAt)
	// hmac_key write-only — preserved from plan.
}
