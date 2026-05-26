// Package agent_pool implements the terrapod_agent_pool resource.
//
// API Contract (Terrapod API <-> Terraform Provider):
//
//	JSON:API type: "agent-pools"
//	ID prefix: "apool-"
//	Create:  POST   /api/terrapod/v1/agent-pools
//	Read:    GET    /api/terrapod/v1/agent-pools/{id}
//	Update:  PATCH  /api/terrapod/v1/agent-pools/{id}
//	Delete:  DELETE /api/terrapod/v1/agent-pools/{id}
//
// Attribute mapping (JSON:API attribute -> Terraform schema attribute):
//
//	"name"                 -> name                 (string, required)
//	"description"          -> description          (string, optional)
//	"labels"               -> labels               (map[string]string, optional)
//	"owner-email"          -> owner_email          (string, optional)
//
// Read-only attributes:
//
//	"created-at"           -> created_at           (string, computed)
//	"updated-at"           -> updated_at           (string, computed)
//
// Import: by agent pool ID (e.g. "apool-abc123").
package agent_pool

import (
	"context"
	"errors"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/mapplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var (
	_ resource.Resource                = &agentPoolResource{}
	_ resource.ResourceWithImportState = &agentPoolResource{}
)

// agentPoolModel maps the Terraform schema to Go types.
type agentPoolModel struct {
	ID types.String `tfsdk:"id"`

	// Writable attributes
	Name        types.String `tfsdk:"name"`
	Description types.String `tfsdk:"description"`
	Labels      types.Map    `tfsdk:"labels"`
	OwnerEmail  types.String `tfsdk:"owner_email"`

	// Read-only attributes
	CreatedAt types.String `tfsdk:"created_at"`
	UpdatedAt types.String `tfsdk:"updated_at"`
}

type agentPoolResource struct {
	client *client.Client
	tc     *terrapod.Client
}

// NewResource returns a new agent pool resource.
func NewResource() resource.Resource {
	return &agentPoolResource{}
}

func (r *agentPoolResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_agent_pool"
}

func (r *agentPoolResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a Terrapod agent pool.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description: "The agent pool ID (e.g. apool-abc123).",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"name": schema.StringAttribute{
				Description: "The name of the agent pool.",
				Required:    true,
			},
			"description": schema.StringAttribute{
				Description: "A description of the agent pool.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"labels": schema.MapAttribute{
				Description: "Labels for RBAC-based access control.",
				Optional:    true,
				Computed:    true,
				ElementType: types.StringType,
				PlanModifiers: []planmodifier.Map{
					mapplanmodifier.UseStateForUnknown(),
				},
			},
			"owner_email": schema.StringAttribute{
				Description: "Email of the pool owner (granted admin permission).",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},

			// Read-only
			"created_at": schema.StringAttribute{
				Description: "Creation timestamp.",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"updated_at": schema.StringAttribute{
				Description: "Last update timestamp.",
				Computed:    true,
			},
		},
	}
}

func (r *agentPoolResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *agentPoolResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan agentPoolModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	p, err := r.tc.CreateAgentPool(ctx, buildCreateAgentPoolRequest(&plan))
	if err != nil {
		resp.Diagnostics.AddError("Failed to create agent pool", err.Error())
		return
	}

	resp.Diagnostics.Append(readAgentPoolFromSDK(ctx, p, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *agentPoolResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state agentPoolModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	p, err := r.tc.GetAgentPool(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Failed to read agent pool", err.Error())
		return
	}

	resp.Diagnostics.Append(readAgentPoolFromSDK(ctx, p, &state)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *agentPoolResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan agentPoolModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var state agentPoolModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	p, err := r.tc.UpdateAgentPool(ctx, state.ID.ValueString(), buildUpdateAgentPoolRequest(&plan))
	if err != nil {
		resp.Diagnostics.AddError("Failed to update agent pool", err.Error())
		return
	}

	resp.Diagnostics.Append(readAgentPoolFromSDK(ctx, p, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *agentPoolResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state agentPoolModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	err := r.tc.DeleteAgentPool(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Failed to delete agent pool", err.Error())
		}
	}
}

func (r *agentPoolResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

// buildCreateAgentPoolRequest projects the Terraform plan into the
// SDK's typed create request. Optional fields are sent only when set.
func buildCreateAgentPoolRequest(m *agentPoolModel) terrapod.CreateAgentPoolRequest {
	req := terrapod.CreateAgentPoolRequest{
		Name: m.Name.ValueString(),
	}
	if !m.Description.IsNull() && !m.Description.IsUnknown() {
		req.Description = m.Description.ValueString()
	}
	if !m.Labels.IsNull() && !m.Labels.IsUnknown() {
		labels := map[string]string{}
		for k, v := range m.Labels.Elements() {
			labels[k] = v.(types.String).ValueString()
		}
		req.Labels = labels
	}
	if !m.OwnerEmail.IsNull() && !m.OwnerEmail.IsUnknown() {
		req.OwnerEmail = m.OwnerEmail.ValueString()
	}
	return req
}

// buildUpdateAgentPoolRequest mirrors the create build but uses the
// SDK's pointer-typed partial-update shape. Terraform always supplies
// every attribute on Update (plan vs state diff is the framework's
// job), so we set every pointer — the SDK serialises only non-nil
// values. The Labels map sets &{} explicitly (clear) when the model
// has no labels, matching the old behaviour where omitting labels in
// HCL cleared them.
func buildUpdateAgentPoolRequest(m *agentPoolModel) terrapod.UpdateAgentPoolRequest {
	req := terrapod.UpdateAgentPoolRequest{
		Name: m.Name.ValueString(),
	}
	if !m.Description.IsNull() && !m.Description.IsUnknown() {
		d := m.Description.ValueString()
		req.Description = &d
	}
	if !m.Labels.IsNull() && !m.Labels.IsUnknown() {
		labels := map[string]string{}
		for k, v := range m.Labels.Elements() {
			labels[k] = v.(types.String).ValueString()
		}
		req.Labels = &labels
	}
	if !m.OwnerEmail.IsNull() && !m.OwnerEmail.IsUnknown() {
		o := m.OwnerEmail.ValueString()
		req.OwnerEmail = &o
	}
	return req
}

// readAgentPoolFromSDK populates the Terraform model from the typed
// SDK shape. Labels round-trip as a Map; empty/missing labels become
// null on the model so a config with no `labels` block matches state.
func readAgentPoolFromSDK(ctx context.Context, p *terrapod.AgentPool, m *agentPoolModel) diag.Diagnostics {
	var diags diag.Diagnostics

	m.ID = types.StringValue(p.ID)
	m.Name = types.StringValue(p.Name)

	if p.Description != "" {
		m.Description = types.StringValue(p.Description)
	} else {
		m.Description = types.StringNull()
	}

	if len(p.Labels) > 0 {
		val, d := types.MapValueFrom(ctx, types.StringType, p.Labels)
		diags.Append(d...)
		m.Labels = val
	} else {
		m.Labels = types.MapNull(types.StringType)
	}

	if p.OwnerEmail != "" {
		m.OwnerEmail = types.StringValue(p.OwnerEmail)
	} else {
		m.OwnerEmail = types.StringNull()
	}

	m.CreatedAt = types.StringValue(p.CreatedAt)
	m.UpdatedAt = types.StringValue(p.UpdatedAt)

	return diags
}
