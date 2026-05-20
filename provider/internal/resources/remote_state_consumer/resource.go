package remote_state_consumer

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var _ resource.Resource = &remoteStateConsumerResource{}

type remoteStateConsumerResource struct {
	client *client.Client
}

func NewResource() resource.Resource {
	return &remoteStateConsumerResource{}
}

func (r *remoteStateConsumerResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_remote_state_consumer"
}

func (r *remoteStateConsumerResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Authorizes a consumer workspace's agent runs to read a producer workspace's state via terraform_remote_state. Producer-controlled allowlist (#344): mutations require admin on the PRODUCER workspace — a consumer team cannot self-grant. State data is secret-bearing; only grant deliberately.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed: true, Description: "Edge ID.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"producer_workspace_id": schema.StringAttribute{
				Required: true, Description: "Producer workspace ID (the workspace whose state will be readable).",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"consumer_workspace_id": schema.StringAttribute{
				Required: true, Description: "Consumer workspace ID (the workspace authorized to read).",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"producer_workspace_name": schema.StringAttribute{
				Computed: true, Description: "Producer workspace name.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"consumer_workspace_name": schema.StringAttribute{
				Computed: true, Description: "Consumer workspace name.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"created_at": schema.StringAttribute{
				Computed: true, Description: "Creation timestamp.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"created_by": schema.StringAttribute{
				Computed: true, Description: "Identity that created the grant.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
		},
	}
}

func (r *remoteStateConsumerResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *remoteStateConsumerResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan remoteStateConsumerModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	rels := map[string]any{
		"consumer": map[string]any{
			"data": map[string]any{
				"id":   plan.ConsumerWorkspaceID.ValueString(),
				"type": "workspaces",
			},
		},
	}

	body, err := client.MarshalResource("remote-state-consumers", map[string]any{}, rels)
	if err != nil {
		resp.Diagnostics.AddError("Marshal error", err.Error())
		return
	}

	data, err := r.client.Post(
		ctx,
		fmt.Sprintf("/api/terrapod/v1/workspaces/%s/remote-state-consumers", plan.ProducerWorkspaceID.ValueString()),
		body,
	)
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

func (r *remoteStateConsumerResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state remoteStateConsumerModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	data, err := r.client.Get(ctx, "/api/terrapod/v1/remote-state-consumers/"+state.ID.ValueString())
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

func (r *remoteStateConsumerResource) Update(_ context.Context, _ resource.UpdateRequest, resp *resource.UpdateResponse) {
	resp.Diagnostics.AddError("Update not supported", "Remote-state consumer grants are immutable. Delete and recreate instead.")
}

func (r *remoteStateConsumerResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state remoteStateConsumerModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	err := r.client.Delete(ctx, "/api/terrapod/v1/remote-state-consumers/"+state.ID.ValueString())
	if err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Delete failed", err.Error())
	}
}

func readIntoModel(res *client.Resource, m *remoteStateConsumerModel) {
	m.ID = types.StringValue(res.ID)
	m.ProducerWorkspaceName = types.StringValue(client.GetStringAttr(res, "producer-workspace-name"))
	m.ConsumerWorkspaceName = types.StringValue(client.GetStringAttr(res, "consumer-workspace-name"))
	m.CreatedAt = types.StringValue(client.GetStringAttr(res, "created-at"))
	m.CreatedBy = types.StringValue(client.GetStringAttr(res, "created-by"))

	if v := client.GetRelationshipID(res, "producer"); v != "" {
		m.ProducerWorkspaceID = types.StringValue(v)
	}
	if v := client.GetRelationshipID(res, "consumer"); v != "" {
		m.ConsumerWorkspaceID = types.StringValue(v)
	}
}
