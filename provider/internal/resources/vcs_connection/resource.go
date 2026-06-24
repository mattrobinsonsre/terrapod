// Package vcs_connection implements the terrapod_vcs_connection resource.
//
// API Contract (Terrapod API <-> Terraform Provider):
//
//	JSON:API type: "vcs-connections"
//	ID prefix: "vcs-"
//	Create:  POST   /api/terrapod/v1/vcs-connections
//	Read:    GET    /api/terrapod/v1/vcs-connections/{id}
//	Delete:  DELETE /api/terrapod/v1/vcs-connections/{id}
//
// This resource is immutable from the Terraform side — any attribute
// change forces replacement. The Terrapod API does support PATCH
// (#315) but the provider has historically modelled VCS connections
// as RequiresReplace because rotating a private key cleanly via
// Terraform plans is messy. The go-terrapod SDK exposes the PATCH
// path for direct callers (CLI tooling, migration tool).
//
// Migrated to go-terrapod (#347): CRUD goes through the typed SDK;
// the legacy *client.Client is kept only because the provider's
// Configure callback hands it to us.
package vcs_connection

import (
	"context"
	"errors"
	"fmt"

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
	_ resource.Resource                = &vcsConnectionResource{}
	_ resource.ResourceWithImportState = &vcsConnectionResource{}
)

type vcsConnectionModel struct {
	ID types.String `tfsdk:"id"`

	Name                 types.String `tfsdk:"name"`
	Provider             types.String `tfsdk:"vcs_provider"`
	ServerURL            types.String `tfsdk:"server_url"`
	GithubAppID          types.Int64  `tfsdk:"github_app_id"`
	GithubInstallationID types.Int64  `tfsdk:"github_installation_id"`
	PrivateKey           types.String `tfsdk:"private_key"`
	Token                types.String `tfsdk:"token"`
	WebhookSecret        types.String `tfsdk:"webhook_secret"`

	Status             types.String `tfsdk:"status"`
	HasToken           types.Bool   `tfsdk:"has_token"`
	HasWebhookSecret   types.Bool   `tfsdk:"has_webhook_secret"`
	GithubAccountLogin types.String `tfsdk:"github_account_login"`
	GithubAccountType  types.String `tfsdk:"github_account_type"`
	CreatedAt          types.String `tfsdk:"created_at"`
	UpdatedAt          types.String `tfsdk:"updated_at"`
}

type vcsConnectionResource struct {
	client *client.Client
	tc     *terrapod.Client
}

func NewResource() resource.Resource {
	return &vcsConnectionResource{}
}

func (r *vcsConnectionResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_vcs_connection"
}

func (r *vcsConnectionResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a Terrapod VCS connection. This resource is immutable — any change forces replacement.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description: "The VCS connection ID (e.g. vcs-abc123).",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"name": schema.StringAttribute{
				Description: "The name of the VCS connection. Changing this forces a new resource.",
				Required:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"vcs_provider": schema.StringAttribute{
				Description: `The VCS provider type: "github" or "gitlab". Changing this forces a new resource.`,
				Required:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"server_url": schema.StringAttribute{
				Description: "The VCS server URL (e.g. https://github.example.com). Defaults to the provider's public URL. Changing this forces a new resource.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
					stringplanmodifier.RequiresReplace(),
				},
			},
			"github_app_id": schema.Int64Attribute{
				Description: "GitHub App ID. Required for GitHub connections. Changing this forces a new resource.",
				Optional:    true,
				PlanModifiers: []planmodifier.Int64{
					int64planmodifier.RequiresReplace(),
				},
			},
			"github_installation_id": schema.Int64Attribute{
				Description: "GitHub App installation ID. Required for GitHub connections. Changing this forces a new resource.",
				Optional:    true,
				PlanModifiers: []planmodifier.Int64{
					int64planmodifier.RequiresReplace(),
				},
			},
			"private_key": schema.StringAttribute{
				Description: "GitHub App private key (PEM). Write-only; never returned by the API. Changing this forces a new resource.",
				Optional:    true,
				Sensitive:   true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"token": schema.StringAttribute{
				Description: "GitLab access token. Write-only; never returned by the API. Changing this forces a new resource.",
				Optional:    true,
				Sensitive:   true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"webhook_secret": schema.StringAttribute{
				Description: "Optional per-connection GitHub webhook HMAC secret. Write-only; never returned by the API. When set, this connection's inbound webhooks are validated against it instead of the global secret. Changing this forces a new resource (rotate in-place via the Terrapod CLI / API).",
				Optional:    true,
				Sensitive:   true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},

			"status": schema.StringAttribute{
				Description: "The connection status.",
				Computed:    true,
			},
			"has_token": schema.BoolAttribute{
				Description: "Whether the connection has a token/key configured.",
				Computed:    true,
			},
			"has_webhook_secret": schema.BoolAttribute{
				Description: "Whether the connection has a per-connection webhook secret configured.",
				Computed:    true,
			},
			"github_account_login": schema.StringAttribute{
				Description: "The GitHub account login (for GitHub connections).",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"github_account_type": schema.StringAttribute{
				Description: "The GitHub account type (for GitHub connections).",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
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

func (r *vcsConnectionResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *vcsConnectionResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan vcsConnectionModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	v, err := r.tc.CreateVCSConnection(ctx, buildCreateVCSConnectionRequest(&plan))
	if err != nil {
		resp.Diagnostics.AddError("Failed to create VCS connection", err.Error())
		return
	}

	readVCSConnectionFromSDK(v, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *vcsConnectionResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state vcsConnectionModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	v, err := r.tc.GetVCSConnection(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Failed to read VCS connection", err.Error())
		return
	}

	// Preserve write-only fields from state — the API never echoes them back.
	privateKey := state.PrivateKey
	token := state.Token
	webhookSecret := state.WebhookSecret

	readVCSConnectionFromSDK(v, &state)
	state.PrivateKey = privateKey
	state.Token = token
	state.WebhookSecret = webhookSecret
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

// Update is not supported — all schema attributes force replacement.
// Required by the framework interface but never reached at runtime.
func (r *vcsConnectionResource) Update(_ context.Context, _ resource.UpdateRequest, resp *resource.UpdateResponse) {
	resp.Diagnostics.AddError(
		"Update not supported",
		"VCS connections are immutable on the Terraform provider — all attributes force replacement. Use the Terrapod CLI / API directly to rotate credentials in-place.",
	)
}

func (r *vcsConnectionResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state vcsConnectionModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	err := r.tc.DeleteVCSConnection(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Failed to delete VCS connection", err.Error())
		}
	}
}

func (r *vcsConnectionResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

// buildCreateVCSConnectionRequest projects the Terraform model into
// the SDK's typed request shape. Optional fields are passed through
// only when set; the SDK drops zero values from the wire body.
func buildCreateVCSConnectionRequest(m *vcsConnectionModel) terrapod.CreateVCSConnectionRequest {
	req := terrapod.CreateVCSConnectionRequest{
		Name:     m.Name.ValueString(),
		Provider: m.Provider.ValueString(),
	}
	if !m.ServerURL.IsNull() && !m.ServerURL.IsUnknown() {
		req.ServerURL = m.ServerURL.ValueString()
	}
	if !m.GithubAppID.IsNull() && !m.GithubAppID.IsUnknown() {
		req.GithubAppID = m.GithubAppID.ValueInt64()
	}
	if !m.GithubInstallationID.IsNull() && !m.GithubInstallationID.IsUnknown() {
		req.GithubInstallationID = m.GithubInstallationID.ValueInt64()
	}
	if !m.PrivateKey.IsNull() && !m.PrivateKey.IsUnknown() {
		req.PrivateKey = m.PrivateKey.ValueString()
	}
	if !m.Token.IsNull() && !m.Token.IsUnknown() {
		req.Token = m.Token.ValueString()
	}
	if !m.WebhookSecret.IsNull() && !m.WebhookSecret.IsUnknown() {
		req.WebhookSecret = m.WebhookSecret.ValueString()
	}
	return req
}

// readVCSConnectionFromSDK populates the Terraform model from the SDK
// type. PrivateKey and Token are write-only — the caller preserves
// them from prior state.
func readVCSConnectionFromSDK(v *terrapod.VCSConnection, m *vcsConnectionModel) {
	m.ID = types.StringValue(v.ID)
	m.Name = types.StringValue(v.Name)
	m.Provider = types.StringValue(v.Provider)

	if v.ServerURL != "" {
		m.ServerURL = types.StringValue(v.ServerURL)
	} else {
		m.ServerURL = types.StringNull()
	}
	if v.GithubAppID != 0 {
		m.GithubAppID = types.Int64Value(v.GithubAppID)
	} else {
		m.GithubAppID = types.Int64Null()
	}
	if v.GithubInstallationID != 0 {
		m.GithubInstallationID = types.Int64Value(v.GithubInstallationID)
	} else {
		m.GithubInstallationID = types.Int64Null()
	}

	m.Status = types.StringValue(v.Status)
	m.HasToken = types.BoolValue(v.HasToken)
	m.HasWebhookSecret = types.BoolValue(v.HasWebhookSecret)

	if v.GithubAccountLogin != "" {
		m.GithubAccountLogin = types.StringValue(v.GithubAccountLogin)
	} else {
		m.GithubAccountLogin = types.StringNull()
	}
	if v.GithubAccountType != "" {
		m.GithubAccountType = types.StringValue(v.GithubAccountType)
	} else {
		m.GithubAccountType = types.StringNull()
	}

	m.CreatedAt = types.StringValue(v.CreatedAt)
	m.UpdatedAt = types.StringValue(v.UpdatedAt)
}
