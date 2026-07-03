// Package execution_hook_workspace implements the terrapod_execution_hook_workspace
// resource (#619) — associates an execution hook with a workspace.
package execution_hook_workspace

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

type executionHookWorkspaceModel struct {
	ID          types.String `tfsdk:"id"`
	HookID      types.String `tfsdk:"hook_id"`
	WorkspaceID types.String `tfsdk:"workspace_id"`
}

var (
	_ resource.Resource                = &executionHookWorkspaceResource{}
	_ resource.ResourceWithImportState = &executionHookWorkspaceResource{}
)

type executionHookWorkspaceResource struct {
	client *client.Client
	tc     *terrapod.Client
}

func NewResource() resource.Resource { return &executionHookWorkspaceResource{} }

func (r *executionHookWorkspaceResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_execution_hook_workspace"
}

func (r *executionHookWorkspaceResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Associates a Terrapod execution hook with a workspace so the hook runs on that workspace's agent runs.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed: true, Description: "Composite ID (hook_id/workspace_id).",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"hook_id": schema.StringAttribute{
				Required: true, Description: "Execution hook ID.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"workspace_id": schema.StringAttribute{
				Required: true, Description: "Workspace ID to associate the hook with.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
		},
	}
}

func (r *executionHookWorkspaceResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *executionHookWorkspaceResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan executionHookWorkspaceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.tc.AssignWorkspaceToExecutionHook(ctx, plan.HookID.ValueString(), plan.WorkspaceID.ValueString()); err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}
	plan.ID = types.StringValue(plan.HookID.ValueString() + "/" + plan.WorkspaceID.ValueString())
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *executionHookWorkspaceResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state executionHookWorkspaceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	assigned, err := r.tc.IsWorkspaceAssignedToExecutionHook(ctx,
		state.HookID.ValueString(),
		state.WorkspaceID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}
	if !assigned {
		resp.State.RemoveResource(ctx)
		return
	}
	state.ID = types.StringValue(state.HookID.ValueString() + "/" + state.WorkspaceID.ValueString())
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *executionHookWorkspaceResource) Update(_ context.Context, _ resource.UpdateRequest, resp *resource.UpdateResponse) {
	resp.Diagnostics.AddError("Update not supported", "Execution hook workspace associations are immutable. Changes require replacement.")
}

func (r *executionHookWorkspaceResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state executionHookWorkspaceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	err := r.tc.UnassignWorkspaceFromExecutionHook(ctx,
		state.HookID.ValueString(),
		state.WorkspaceID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Delete failed", err.Error())
		}
	}
}

func (r *executionHookWorkspaceResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	parts := strings.SplitN(req.ID, "/", 2)
	if len(parts) != 2 {
		resp.Diagnostics.AddError("Invalid import ID", "Expected format: hook_id/workspace_id")
		return
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("hook_id"), parts[0])...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("workspace_id"), parts[1])...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), req.ID)...)
}
