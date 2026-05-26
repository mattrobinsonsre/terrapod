package remote_state_consumer

import (
	"context"
	"errors"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var _ resource.Resource = &remoteStateConsumerResource{}

type remoteStateConsumerResource struct {
	client *client.Client
	tc     *terrapod.Client
}

func NewResource() resource.Resource {
	return &remoteStateConsumerResource{}
}

func (r *remoteStateConsumerResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_remote_state_consumer"
}

func (r *remoteStateConsumerResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Authorizes a consumer workspace's agent runs to read a producer workspace's state via terraform_remote_state. Producer-controlled allowlist (#344): mutations require admin on the PRODUCER workspace.",
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

	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: c.BaseURL, Token: c.Token})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	r.tc = tc
}

func (r *remoteStateConsumerResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan remoteStateConsumerModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	rsc, err := r.tc.CreateRemoteStateConsumer(ctx, terrapod.CreateRemoteStateConsumerRequest{
		ProducerWorkspaceID: plan.ProducerWorkspaceID.ValueString(),
		ConsumerWorkspaceID: plan.ConsumerWorkspaceID.ValueString(),
	})
	if err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}

	readRSCFromSDK(rsc, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *remoteStateConsumerResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state remoteStateConsumerModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	rsc, err := r.tc.GetRemoteStateConsumer(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}

	readRSCFromSDK(rsc, &state)
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

	err := r.tc.DeleteRemoteStateConsumer(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Delete failed", err.Error())
		}
	}
}

func readRSCFromSDK(rsc *terrapod.RemoteStateConsumer, m *remoteStateConsumerModel) {
	m.ID = types.StringValue(rsc.ID)
	m.ProducerWorkspaceName = types.StringValue(rsc.ProducerWorkspaceName)
	m.ConsumerWorkspaceName = types.StringValue(rsc.ConsumerWorkspaceName)
	m.CreatedAt = types.StringValue(rsc.CreatedAt)
	m.CreatedBy = types.StringValue(rsc.CreatedBy)
	if rsc.ProducerWorkspaceID != "" {
		m.ProducerWorkspaceID = types.StringValue(rsc.ProducerWorkspaceID)
	}
	if rsc.ConsumerWorkspaceID != "" {
		m.ConsumerWorkspaceID = types.StringValue(rsc.ConsumerWorkspaceID)
	}
}
