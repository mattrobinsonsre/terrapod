// Package agent_pool implements the terrapod_agent_pool resource.
//
// API Contract (Terrapod API <-> Terraform Provider):
//
//	JSON:API type: "agent-pools"
//	ID prefix: "apool-"
//	Create:  POST   /api/v2/organizations/default/agent-pools
//	Read:    GET    /api/v2/agent-pools/{id}
//	Update:  PATCH  /api/v2/agent-pools/{id}
//	Delete:  DELETE /api/v2/agent-pools/{id}
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
	"encoding/json"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/mapplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

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
}

func (r *agentPoolResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan agentPoolModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs := buildAgentPoolAttrs(&plan)

	body, err := client.MarshalResource("agent-pools", attrs, nil)
	if err != nil {
		resp.Diagnostics.AddError("Failed to marshal request", err.Error())
		return
	}

	data, err := r.client.Post(ctx, "/api/v2/organizations/default/agent-pools", body)
	if err != nil {
		resp.Diagnostics.AddError("Failed to create agent pool", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	resp.Diagnostics.Append(readAgentPoolIntoModel(ctx, res, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *agentPoolResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state agentPoolModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	data, err := r.client.Get(ctx, "/api/v2/agent-pools/"+state.ID.ValueString())
	if err != nil {
		if client.IsNotFound(err) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Failed to read agent pool", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	resp.Diagnostics.Append(readAgentPoolIntoModel(ctx, res, &state)...)
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

	attrs := buildAgentPoolAttrs(&plan)

	body, err := client.MarshalResourceWithID(state.ID.ValueString(), "agent-pools", attrs)
	if err != nil {
		resp.Diagnostics.AddError("Failed to marshal request", err.Error())
		return
	}

	data, err := r.client.Patch(ctx, "/api/v2/agent-pools/"+state.ID.ValueString(), body)
	if err != nil {
		resp.Diagnostics.AddError("Failed to update agent pool", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	resp.Diagnostics.Append(readAgentPoolIntoModel(ctx, res, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *agentPoolResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state agentPoolModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	err := r.client.Delete(ctx, "/api/v2/agent-pools/"+state.ID.ValueString())
	if err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Failed to delete agent pool", err.Error())
	}
}

func (r *agentPoolResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

// buildAgentPoolAttrs converts the Terraform model into JSON:API attributes.
func buildAgentPoolAttrs(m *agentPoolModel) map[string]any {
	attrs := map[string]any{
		"name": m.Name.ValueString(),
	}

	if !m.Description.IsNull() && !m.Description.IsUnknown() {
		attrs["description"] = m.Description.ValueString()
	}

	if !m.Labels.IsNull() && !m.Labels.IsUnknown() {
		labels := map[string]string{}
		for k, v := range m.Labels.Elements() {
			labels[k] = v.(types.String).ValueString()
		}
		attrs["labels"] = labels
	}

	if !m.OwnerEmail.IsNull() && !m.OwnerEmail.IsUnknown() {
		attrs["owner-email"] = m.OwnerEmail.ValueString()
	}

	return attrs
}

// readAgentPoolIntoModel populates the Terraform model from a JSON:API resource.
func readAgentPoolIntoModel(ctx context.Context, res *client.Resource, m *agentPoolModel) diag.Diagnostics {
	var diags diag.Diagnostics

	m.ID = types.StringValue(res.ID)
	m.Name = types.StringValue(client.GetStringAttr(res, "name"))

	if v := client.GetStringAttr(res, "description"); v != "" {
		m.Description = types.StringValue(v)
	} else {
		m.Description = types.StringNull()
	}

	// Labels — treat empty map {} as a valid value (not null) to avoid
	// unnecessary Terraform diffs between config `labels = {}` and state `null`.
	if raw, ok := res.Attributes["labels"]; ok && len(raw) > 0 {
		var labels map[string]string
		if err := json.Unmarshal(raw, &labels); err == nil {
			val, d := types.MapValueFrom(ctx, types.StringType, labels)
			diags.Append(d...)
			m.Labels = val
		} else {
			m.Labels = types.MapNull(types.StringType)
		}
	} else {
		m.Labels = types.MapNull(types.StringType)
	}

	// Owner email
	if v := client.GetStringAttr(res, "owner-email"); v != "" {
		m.OwnerEmail = types.StringValue(v)
	} else {
		m.OwnerEmail = types.StringNull()
	}

	m.CreatedAt = types.StringValue(client.GetStringAttr(res, "created-at"))
	m.UpdatedAt = types.StringValue(client.GetStringAttr(res, "updated-at"))

	return diags
}
