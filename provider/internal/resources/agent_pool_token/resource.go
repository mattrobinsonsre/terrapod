// Package agent_pool_token implements the terrapod_agent_pool_token resource.
//
// API Contract (Terrapod API <-> Terraform Provider):
//
//	JSON:API type: "authentication-tokens"
//	ID prefix: "at-"
//	Create:  POST   /api/v2/agent-pools/{pool_id}/tokens
//	Read:    GET    /api/v2/agent-pools/{pool_id}/tokens (list, find by ID)
//	Delete:  DELETE /api/v2/agent-pools/{pool_id}/tokens/{token_id}
//
// This resource is immutable — there is no update (PATCH) endpoint.
// Any attribute change forces replacement.
//
// Attribute mapping (JSON:API attribute -> Terraform schema attribute):
//
//	"description"  -> description  (string, optional, forces new)
//	"max-uses"     -> max_uses     (int,    optional, forces new)
//	"expires-at"   -> expires_at   (string, optional, forces new)
//
// Non-API attributes:
//
//	pool_id -> pool_id (string, required, forces new — used to construct API paths)
//
// Read-only attributes:
//
//	"token"        -> token        (string, sensitive — returned ONLY on create)
//	"is-revoked"   -> is_revoked   (bool,   computed)
//	"use-count"    -> use_count    (int,    computed)
//	"created-at"   -> created_at   (string, computed)
//	"created-by"   -> created_by   (string, computed)
//
// Import: "pool_id/token_id" (e.g. "apool-abc123/at-def456").
// Note: the raw token value is not available after import.
package agent_pool_token

import (
	"context"
	"fmt"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var (
	_ resource.Resource                = &agentPoolTokenResource{}
	_ resource.ResourceWithImportState = &agentPoolTokenResource{}
)

// agentPoolTokenModel maps the Terraform schema to Go types.
type agentPoolTokenModel struct {
	ID types.String `tfsdk:"id"`

	// Writable attributes (all force replacement — resource is immutable)
	PoolID      types.String `tfsdk:"pool_id"`
	Description types.String `tfsdk:"description"`
	MaxUses     types.Int64  `tfsdk:"max_uses"`
	ExpiresAt   types.String `tfsdk:"expires_at"`

	// Read-only attributes
	Token     types.String `tfsdk:"token"`
	IsRevoked types.Bool   `tfsdk:"is_revoked"`
	UseCount  types.Int64  `tfsdk:"use_count"`
	CreatedAt types.String `tfsdk:"created_at"`
	CreatedBy types.String `tfsdk:"created_by"`
}

type agentPoolTokenResource struct {
	client *client.Client
}

// NewResource returns a new agent pool token resource.
func NewResource() resource.Resource {
	return &agentPoolTokenResource{}
}

func (r *agentPoolTokenResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_agent_pool_token"
}

func (r *agentPoolTokenResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a Terrapod agent pool token. This resource is immutable — any change forces replacement. The raw token value is only available at creation time.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description: "The token ID (e.g. at-abc123).",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"pool_id": schema.StringAttribute{
				Description: "The agent pool ID this token belongs to. Changing this forces a new resource.",
				Required:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"description": schema.StringAttribute{
				Description: "A description for the token. Changing this forces a new resource.",
				Optional:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"max_uses": schema.Int64Attribute{
				Description: "Maximum number of times this token can be used. Changing this forces a new resource.",
				Optional:    true,
				PlanModifiers: []planmodifier.Int64{
					int64planmodifier.RequiresReplace(),
				},
			},
			"expires_at": schema.StringAttribute{
				Description: "Expiration timestamp (RFC3339). Changing this forces a new resource.",
				Optional:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},

			// Read-only
			"token": schema.StringAttribute{
				Description: "The raw token value. Only available at creation time; not returned on subsequent reads or after import.",
				Computed:    true,
				Sensitive:   true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"is_revoked": schema.BoolAttribute{
				Description: "Whether the token has been revoked.",
				Computed:    true,
			},
			"use_count": schema.Int64Attribute{
				Description: "Number of times this token has been used.",
				Computed:    true,
			},
			"created_at": schema.StringAttribute{
				Description: "Creation timestamp.",
				Computed:    true,
			},
			"created_by": schema.StringAttribute{
				Description: "Email of the user who created the token.",
				Computed:    true,
			},
		},
	}
}

func (r *agentPoolTokenResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *agentPoolTokenResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan agentPoolTokenModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs := buildAgentPoolTokenAttrs(&plan)

	body, err := client.MarshalResource("authentication-tokens", attrs, nil)
	if err != nil {
		resp.Diagnostics.AddError("Failed to marshal request", err.Error())
		return
	}

	endpoint := "/api/v2/agent-pools/" + plan.PoolID.ValueString() + "/tokens"
	data, err := r.client.Post(ctx, endpoint, body)
	if err != nil {
		resp.Diagnostics.AddError("Failed to create agent pool token", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	readAgentPoolTokenIntoModel(res, &plan)

	// The raw token value is only available in the create response.
	if v := client.GetStringAttr(res, "token"); v != "" {
		plan.Token = types.StringValue(v)
	}

	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *agentPoolTokenResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state agentPoolTokenModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	// The API does not have a single-token GET endpoint. List all tokens
	// for the pool and find the one matching our ID.
	endpoint := "/api/v2/agent-pools/" + state.PoolID.ValueString() + "/tokens"
	data, err := r.client.Get(ctx, endpoint)
	if err != nil {
		if client.IsNotFound(err) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Failed to list agent pool tokens", err.Error())
		return
	}

	resources, err := client.ParseResourceList(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	// Find our token in the list.
	var found *client.Resource
	for i := range resources {
		if resources[i].ID == state.ID.ValueString() {
			found = &resources[i]
			break
		}
	}

	if found == nil {
		resp.State.RemoveResource(ctx)
		return
	}

	// Preserve the token value from state (API never returns it after create).
	token := state.Token

	readAgentPoolTokenIntoModel(found, &state)
	state.Token = token
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

// Update is not supported — all attributes force replacement.
// This method is required by the Resource interface but should never be called.
func (r *agentPoolTokenResource) Update(_ context.Context, _ resource.UpdateRequest, resp *resource.UpdateResponse) {
	resp.Diagnostics.AddError(
		"Update not supported",
		"Agent pool tokens are immutable. All changes force replacement.",
	)
}

func (r *agentPoolTokenResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state agentPoolTokenModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	endpoint := "/api/v2/agent-pools/" + state.PoolID.ValueString() + "/tokens/" + state.ID.ValueString()
	err := r.client.Delete(ctx, endpoint)
	if err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Failed to delete agent pool token", err.Error())
	}
}

func (r *agentPoolTokenResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	// Import format: "pool_id/token_id" (e.g. "apool-abc123/at-def456").
	parts := strings.SplitN(req.ID, "/", 2)
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		resp.Diagnostics.AddError(
			"Invalid import ID",
			fmt.Sprintf("Expected format: pool_id/token_id (e.g. apool-abc123/at-def456), got: %q", req.ID),
		)
		return
	}

	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("pool_id"), parts[0])...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), parts[1])...)
}

// buildAgentPoolTokenAttrs converts the Terraform model into JSON:API attributes.
func buildAgentPoolTokenAttrs(m *agentPoolTokenModel) map[string]any {
	attrs := map[string]any{}

	if !m.Description.IsNull() && !m.Description.IsUnknown() {
		attrs["description"] = m.Description.ValueString()
	}
	if !m.MaxUses.IsNull() && !m.MaxUses.IsUnknown() {
		attrs["max-uses"] = m.MaxUses.ValueInt64()
	}
	if !m.ExpiresAt.IsNull() && !m.ExpiresAt.IsUnknown() {
		attrs["expires-at"] = m.ExpiresAt.ValueString()
	}

	return attrs
}

// readAgentPoolTokenIntoModel populates the Terraform model from a JSON:API resource.
// Note: the raw "token" attribute is NOT read here — it is only available on create
// and must be handled separately by the caller.
func readAgentPoolTokenIntoModel(res *client.Resource, m *agentPoolTokenModel) {
	m.ID = types.StringValue(res.ID)

	// pool_id is not returned in the resource attributes — preserve from state/plan.

	if v := client.GetStringAttr(res, "description"); v != "" {
		m.Description = types.StringValue(v)
	} else {
		m.Description = types.StringNull()
	}

	if v := client.GetIntAttr(res, "max-uses"); v != 0 {
		m.MaxUses = types.Int64Value(v)
	} else {
		m.MaxUses = types.Int64Null()
	}

	if v := client.GetStringAttr(res, "expires-at"); v != "" {
		m.ExpiresAt = types.StringValue(v)
	} else {
		m.ExpiresAt = types.StringNull()
	}

	m.IsRevoked = types.BoolValue(client.GetBoolAttr(res, "is-revoked"))
	m.UseCount = types.Int64Value(client.GetIntAttr(res, "use-count"))
	m.CreatedAt = types.StringValue(client.GetStringAttr(res, "created-at"))

	if v := client.GetStringAttr(res, "created-by"); v != "" {
		m.CreatedBy = types.StringValue(v)
	} else {
		m.CreatedBy = types.StringNull()
	}
}
