package module_workspace_link

import (
	"context"
	"fmt"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var (
	_ resource.Resource                = &moduleWorkspaceLinkResource{}
	_ resource.ResourceWithImportState = &moduleWorkspaceLinkResource{}
)

type moduleWorkspaceLinkResource struct {
	client *client.Client
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
			},
			"created_at": schema.StringAttribute{Computed: true, Description: "Creation timestamp."},
			"created_by": schema.StringAttribute{Computed: true, Description: "Creator email."},
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
}

func (r *moduleWorkspaceLinkResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan moduleWorkspaceLinkModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	body, err := client.MarshalResource("workspace-links", map[string]any{
		"workspace_id": plan.WorkspaceID.ValueString(),
	}, nil)
	if err != nil {
		resp.Diagnostics.AddError("Marshal error", err.Error())
		return
	}

	data, err := r.client.Post(ctx, linksPath(plan.ModuleName.ValueString(), plan.ModuleProvider.ValueString()), body)
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

func (r *moduleWorkspaceLinkResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state moduleWorkspaceLinkModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	// No individual GET endpoint — list all links and find ours by ID.
	data, err := r.client.Get(ctx, linksPath(state.ModuleName.ValueString(), state.ModuleProvider.ValueString()))
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

	for i := range resources {
		if resources[i].ID == state.ID.ValueString() {
			readIntoModel(&resources[i], &state)
			resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
			return
		}
	}

	// Link no longer exists.
	resp.State.RemoveResource(ctx)
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

	deletePath := fmt.Sprintf("%s/%s", linksPath(state.ModuleName.ValueString(), state.ModuleProvider.ValueString()), state.ID.ValueString())
	err := r.client.Delete(ctx, deletePath)
	if err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Delete failed", err.Error())
	}
}

func (r *moduleWorkspaceLinkResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	// Import ID format: "module_name/provider_name/link_id"
	parts := strings.SplitN(req.ID, "/", 3)
	if len(parts) != 3 {
		resp.Diagnostics.AddError("Invalid import ID", "Expected format: module_name/provider_name/link_id")
		return
	}

	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("module_name"), parts[0])...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("module_provider"), parts[1])...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), parts[2])...)
}

func linksPath(name, provider string) string {
	return fmt.Sprintf("/api/v2/organizations/default/registry-modules/private/default/%s/%s/workspace-links", name, provider)
}

func readIntoModel(res *client.Resource, m *moduleWorkspaceLinkModel) {
	m.ID = types.StringValue(res.ID)
	m.WorkspaceID = types.StringValue(client.GetStringAttr(res, "workspace-id"))
	m.WorkspaceName = types.StringValue(client.GetStringAttr(res, "workspace-name"))
	m.CreatedAt = types.StringValue(client.GetStringAttr(res, "created-at"))
	m.CreatedBy = types.StringValue(client.GetStringAttr(res, "created-by"))
}
