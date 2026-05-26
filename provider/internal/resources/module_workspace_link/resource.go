package module_workspace_link

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

var (
	_ resource.Resource                = &moduleWorkspaceLinkResource{}
	_ resource.ResourceWithImportState = &moduleWorkspaceLinkResource{}
)

type moduleWorkspaceLinkResource struct {
	client *client.Client
	tc     *terrapod.Client
}

func NewResource() resource.Resource {
	return &moduleWorkspaceLinkResource{}
}

func (r *moduleWorkspaceLinkResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_module_workspace_link"
}

func (r *moduleWorkspaceLinkResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Links a workspace to a registry module for impact analysis. When the module's VCS repository receives a PR, speculative plan-only runs are triggered on linked workspaces.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed: true, Description: "Workspace link ID.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"module_name": schema.StringAttribute{
				Required: true, Description: "Module name (e.g. \"vpc-endpoints\").",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"module_provider": schema.StringAttribute{
				Required: true, Description: "Module provider (e.g. \"aws\").",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"workspace_id": schema.StringAttribute{
				Required: true, Description: "Workspace ID to link.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"workspace_name": schema.StringAttribute{
				Computed: true, Description: "Workspace name.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"created_at": schema.StringAttribute{
				Computed: true, Description: "Creation timestamp.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"created_by": schema.StringAttribute{
				Computed: true, Description: "Creator email.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
		},
	}
}

func (r *moduleWorkspaceLinkResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *moduleWorkspaceLinkResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan moduleWorkspaceLinkModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	mwl, err := r.tc.CreateModuleWorkspaceLink(ctx, terrapod.CreateModuleWorkspaceLinkRequest{
		ModuleName:     plan.ModuleName.ValueString(),
		ModuleProvider: plan.ModuleProvider.ValueString(),
		WorkspaceID:    plan.WorkspaceID.ValueString(),
	})
	if err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}

	readMWLFromSDK(mwl, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *moduleWorkspaceLinkResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state moduleWorkspaceLinkModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	mwl, err := r.tc.GetModuleWorkspaceLink(ctx,
		state.ModuleName.ValueString(),
		state.ModuleProvider.ValueString(),
		state.ID.ValueString(),
	)
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}
	if mwl == nil {
		resp.State.RemoveResource(ctx)
		return
	}

	readMWLFromSDK(mwl, &state)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *moduleWorkspaceLinkResource) Update(_ context.Context, _ resource.UpdateRequest, resp *resource.UpdateResponse) {
	resp.Diagnostics.AddError("Update not supported", "Module workspace links are immutable. Delete and recreate instead.")
}

func (r *moduleWorkspaceLinkResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state moduleWorkspaceLinkModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	err := r.tc.DeleteModuleWorkspaceLink(ctx,
		state.ModuleName.ValueString(),
		state.ModuleProvider.ValueString(),
		state.ID.ValueString(),
	)
	if err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Delete failed", err.Error())
		}
	}
}

func (r *moduleWorkspaceLinkResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	parts := strings.SplitN(req.ID, "/", 3)
	if len(parts) != 3 {
		resp.Diagnostics.AddError("Invalid import ID", "Expected format: module_name/provider_name/link_id")
		return
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("module_name"), parts[0])...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("module_provider"), parts[1])...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), parts[2])...)
}

func readMWLFromSDK(mwl *terrapod.ModuleWorkspaceLink, m *moduleWorkspaceLinkModel) {
	m.ID = types.StringValue(mwl.ID)
	m.WorkspaceID = types.StringValue(mwl.WorkspaceID)
	m.WorkspaceName = types.StringValue(mwl.WorkspaceName)
	m.CreatedAt = types.StringValue(mwl.CreatedAt)
	m.CreatedBy = types.StringValue(mwl.CreatedBy)
}
