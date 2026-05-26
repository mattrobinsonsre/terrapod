// Package agent_pool_token implements the terrapod_agent_pool_token resource.
// Migrated to go-terrapod (#347). Immutable from Terraform — any change forces replace.
package agent_pool_token

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var (
	_ resource.Resource                = &agentPoolTokenResource{}
	_ resource.ResourceWithImportState = &agentPoolTokenResource{}
)

type agentPoolTokenModel struct {
	ID types.String `tfsdk:"id"`

	PoolID      types.String `tfsdk:"pool_id"`
	Description types.String `tfsdk:"description"`
	MaxUses     types.Int64  `tfsdk:"max_uses"`
	ExpiresAt   types.String `tfsdk:"expires_at"`

	Token     types.String `tfsdk:"token"`
	IsRevoked types.Bool   `tfsdk:"is_revoked"`
	UseCount  types.Int64  `tfsdk:"use_count"`
	CreatedAt types.String `tfsdk:"created_at"`
	CreatedBy types.String `tfsdk:"created_by"`
}

type agentPoolTokenResource struct {
	client *client.Client
	tc     *terrapod.Client
}

func NewResource() resource.Resource { return &agentPoolTokenResource{} }

func (r *agentPoolTokenResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_agent_pool_token"
}

func (r *agentPoolTokenResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a Terrapod agent pool token. Immutable — any change forces replacement. The raw token is only available at creation time.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description: "Token ID.", Computed: true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"pool_id": schema.StringAttribute{
				Description: "Agent pool ID this token belongs to.", Required: true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"description": schema.StringAttribute{
				Description: "Description.", Optional: true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"max_uses": schema.Int64Attribute{
				Description: "Max uses.", Optional: true,
				PlanModifiers: []planmodifier.Int64{int64planmodifier.RequiresReplace()},
			},
			"expires_at": schema.StringAttribute{
				// Optional + Computed because the Terrapod API auto-
				// assigns an expiry (typically pool TTL + 1h) when
				// the client doesn't supply one. Without Computed,
				// tofu's PlanResourceChange sees the create-response
				// expiry as drift against the plan's null value and
				// fails with "Provider produced inconsistent result".
				Description: "Expiration timestamp (RFC3339). Server-assigned when omitted.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"token": schema.StringAttribute{
				Description: "Raw token value. Returned only on create.", Computed: true, Sensitive: true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"is_revoked": schema.BoolAttribute{Description: "Revocation flag.", Computed: true},
			"use_count":  schema.Int64Attribute{Description: "Use count.", Computed: true},
			"created_at": schema.StringAttribute{Description: "Creation timestamp.", Computed: true, PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"created_by": schema.StringAttribute{Description: "Creator email.", Computed: true, PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
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
	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: c.BaseURL, Token: c.Token})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	r.tc = tc
}

func (r *agentPoolTokenResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan agentPoolTokenModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	sdkReq := terrapod.CreateAgentPoolTokenRequest{}
	if !plan.Description.IsNull() && !plan.Description.IsUnknown() {
		sdkReq.Description = plan.Description.ValueString()
	}
	if !plan.MaxUses.IsNull() && !plan.MaxUses.IsUnknown() {
		sdkReq.MaxUses = plan.MaxUses.ValueInt64()
	}
	if !plan.ExpiresAt.IsNull() && !plan.ExpiresAt.IsUnknown() {
		sdkReq.ExpiresAt = plan.ExpiresAt.ValueString()
	}

	tok, err := r.tc.CreateAgentPoolToken(ctx, plan.PoolID.ValueString(), sdkReq)
	if err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}

	readPoolTokenFromSDK(tok, &plan)
	if tok.Token != "" {
		plan.Token = types.StringValue(tok.Token)
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *agentPoolTokenResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state agentPoolTokenModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	tok, err := r.tc.GetAgentPoolToken(ctx, state.PoolID.ValueString(), state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}
	if tok == nil {
		resp.State.RemoveResource(ctx)
		return
	}

	// Preserve write-once token from state.
	token := state.Token
	readPoolTokenFromSDK(tok, &state)
	state.Token = token
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *agentPoolTokenResource) Update(_ context.Context, _ resource.UpdateRequest, resp *resource.UpdateResponse) {
	resp.Diagnostics.AddError("Update not supported", "Agent pool tokens are immutable. All changes force replacement.")
}

func (r *agentPoolTokenResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state agentPoolTokenModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	err := r.tc.DeleteAgentPoolToken(ctx, state.PoolID.ValueString(), state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Delete failed", err.Error())
		}
	}
}

func (r *agentPoolTokenResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	parts := strings.SplitN(req.ID, "/", 2)
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		resp.Diagnostics.AddError("Invalid import ID",
			fmt.Sprintf("Expected format: pool_id/token_id, got: %q", req.ID))
		return
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("pool_id"), parts[0])...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), parts[1])...)
}

func readPoolTokenFromSDK(t *terrapod.AgentPoolToken, m *agentPoolTokenModel) {
	m.ID = types.StringValue(t.ID)
	if t.Description != "" {
		m.Description = types.StringValue(t.Description)
	} else {
		m.Description = types.StringNull()
	}
	if t.MaxUses != 0 {
		m.MaxUses = types.Int64Value(t.MaxUses)
	} else {
		m.MaxUses = types.Int64Null()
	}
	if t.ExpiresAt != "" {
		m.ExpiresAt = types.StringValue(t.ExpiresAt)
	} else {
		m.ExpiresAt = types.StringNull()
	}
	m.IsRevoked = types.BoolValue(t.IsRevoked)
	m.UseCount = types.Int64Value(t.UseCount)
	m.CreatedAt = types.StringValue(t.CreatedAt)
	if t.CreatedBy != "" {
		m.CreatedBy = types.StringValue(t.CreatedBy)
	} else {
		m.CreatedBy = types.StringNull()
	}
}
