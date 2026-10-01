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
// Everything that identifies the connection or authenticates it is immutable
// from the Terraform side — changing it forces replacement. The Terrapod API
// does support PATCH (#315) but the provider has historically modelled VCS
// connections as RequiresReplace because rotating a private key cleanly via
// Terraform plans is messy. The go-terrapod SDK exposes the PATCH path for
// direct callers (CLI tooling, migration tool).
//
// Update:  PATCH  /api/terrapod/v1/vcs-connections/{id}
//
// The exception, and it is deliberate: owner_email, labels and
// allowed_repositories update in place (GHSA-v8g7-pqrj-8mcm). Replacement is
// not an option for these. Deleting a VCS connection unlinks every workspace
// that references it, so if tightening a repository allowlist or adding an RBAC
// label destroyed the connection, the cost of using the control would be an
// estate-wide outage — and a security control that expensive to adjust does not
// get adjusted. These three are also exactly what the server models as a
// partial PATCH, so the mapping is direct.
//
// Migrated to go-terrapod (#347): CRUD goes through the typed SDK;
// the legacy *client.Client is kept only because the provider's
// Configure callback hands it to us.
package vcs_connection

import (
	"context"
	"errors"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/listplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/mapplanmodifier"
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

	// Reach and scope. Updatable in place — see the package doc.
	OwnerEmail          types.String `tfsdk:"owner_email"`
	Labels              types.Map    `tfsdk:"labels"`
	AllowedRepositories types.List   `tfsdk:"allowed_repositories"`

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

			// Reach and scope (GHSA-v8g7-pqrj-8mcm). Unlike every other
			// configurable attribute here these do NOT force replacement — see
			// the package doc for why destroying the connection is not an
			// acceptable way to change them.
			"owner_email": schema.StringAttribute{
				Description: "Email of the connection owner, who may reference it from a workspace. Updated in place.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"labels": schema.MapAttribute{
				Description: "Labels for RBAC-based access control: a role whose rules match these may reference the connection from a workspace. Updated in place. Set `{}` to remove every label — omitting the attribute keeps whatever is already set.",
				Optional:    true,
				Computed:    true,
				ElementType: types.StringType,
				PlanModifiers: []planmodifier.Map{
					mapplanmodifier.UseStateForUnknown(),
				},
			},
			"allowed_repositories": schema.ListAttribute{
				Description: "Glob patterns bounding which repositories a workspace may point at through this connection. AN EMPTY LIST MEANS ANY REPOSITORY, so `[]` removes the restriction rather than denying everything. Updated in place. Note that omitting the attribute keeps whatever is already set — widen the scope by setting `[]` explicitly, not by deleting the attribute.",
				Optional:    true,
				Computed:    true,
				ElementType: types.StringType,
				PlanModifiers: []planmodifier.List{
					listplanmodifier.UseStateForUnknown(),
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

	resp.Diagnostics.Append(readVCSConnectionFromSDK(ctx, v, &plan)...)
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

	resp.Diagnostics.Append(readVCSConnectionFromSDK(ctx, v, &state)...)
	state.PrivateKey = privateKey
	state.Token = token
	state.WebhookSecret = webhookSecret
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

// Update patches the three reach/scope attributes — owner_email, labels and
// allowed_repositories — and nothing else. Every other attribute forces
// replacement, so the framework only reaches this when the diff is confined to
// those three. See the package doc for why they are not RequiresReplace: a
// connection's deletion unlinks every workspace using it, which is far too
// expensive a way to add a label or adjust a repository pattern.
func (r *vcsConnectionResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan vcsConnectionModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var state vcsConnectionModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	v, err := r.tc.UpdateVCSConnection(ctx, state.ID.ValueString(), buildUpdateVCSConnectionRequest(&plan))
	if err != nil {
		resp.Diagnostics.AddError("Failed to update VCS connection", err.Error())
		return
	}

	// The API never echoes the credentials back, so carry them across from the
	// plan exactly as Create does — reading them off the response would wipe
	// them from state and make the next plan want to replace the connection.
	privateKey := plan.PrivateKey
	token := plan.Token
	webhookSecret := plan.WebhookSecret

	resp.Diagnostics.Append(readVCSConnectionFromSDK(ctx, v, &plan)...)
	plan.PrivateKey = privateKey
	plan.Token = token
	plan.WebhookSecret = webhookSecret
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
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
	if !m.OwnerEmail.IsNull() && !m.OwnerEmail.IsUnknown() {
		req.OwnerEmail = m.OwnerEmail.ValueString()
	}
	if !m.Labels.IsNull() && !m.Labels.IsUnknown() {
		req.Labels = labelsFromModel(m)
	}
	if !m.AllowedRepositories.IsNull() && !m.AllowedRepositories.IsUnknown() {
		req.AllowedRepositories = reposFromModel(m)
	}
	return req
}

// buildUpdateVCSConnectionRequest projects the plan into the SDK's partial
// PATCH shape, and sends ONLY the three reach/scope attributes. Everything else
// on this resource forces replacement, so Update is unreachable unless the diff
// is confined to these three — including name and the credentials in the body
// would widen a deliberately narrow write for no reason.
//
// An unknown or null planned value is left nil, which omits the key and leaves
// the server's stored value alone. A known value is sent even when empty,
// because an explicit empty value is how the server is told to clear the
// field — and for the allowlist, clearing it is what restores "any repository".
func buildUpdateVCSConnectionRequest(m *vcsConnectionModel) terrapod.UpdateVCSConnectionRequest {
	var req terrapod.UpdateVCSConnectionRequest
	if !m.OwnerEmail.IsNull() && !m.OwnerEmail.IsUnknown() {
		o := m.OwnerEmail.ValueString()
		req.OwnerEmail = &o
	}
	if !m.Labels.IsNull() && !m.Labels.IsUnknown() {
		labels := labelsFromModel(m)
		req.Labels = &labels
	}
	if !m.AllowedRepositories.IsNull() && !m.AllowedRepositories.IsUnknown() {
		repos := reposFromModel(m)
		req.AllowedRepositories = &repos
	}
	return req
}

// labelsFromModel flattens the labels map. It returns a non-nil empty map for
// an empty attribute so the caller can send `{}` rather than `null`.
func labelsFromModel(m *vcsConnectionModel) map[string]string {
	labels := map[string]string{}
	for k, v := range m.Labels.Elements() {
		if s, ok := v.(types.String); ok {
			labels[k] = s.ValueString()
		}
	}
	return labels
}

// reposFromModel flattens the allowlist, returning a non-nil empty slice for an
// empty attribute — `[]` is meaningful on this field and must not become null.
func reposFromModel(m *vcsConnectionModel) []string {
	elems := m.AllowedRepositories.Elements()
	repos := make([]string, 0, len(elems))
	for _, v := range elems {
		if s, ok := v.(types.String); ok {
			repos = append(repos, s.ValueString())
		}
	}
	return repos
}

// readVCSConnectionFromSDK populates the Terraform model from the SDK
// type. PrivateKey and Token are write-only — the caller preserves
// them from prior state.
func readVCSConnectionFromSDK(ctx context.Context, v *terrapod.VCSConnection, m *vcsConnectionModel) diag.Diagnostics {
	var diags diag.Diagnostics

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

	// Reach and scope round-trip FAITHFULLY: an empty value stays an empty
	// value and is never collapsed to null. The older mapping used elsewhere in
	// the provider ("empty means null") cannot be used here, because a config
	// that explicitly asks for `allowed_repositories = []` — the supported way
	// to widen the scope back to any repository — would then plan as [] and
	// apply to null, which the framework rejects as an inconsistent result.
	// Since all three are Computed, an absent config simply takes whatever the
	// server reports.
	m.OwnerEmail = types.StringValue(v.OwnerEmail)

	labels := v.Labels
	if labels == nil {
		labels = map[string]string{}
	}
	labelVal, d := types.MapValueFrom(ctx, types.StringType, labels)
	diags.Append(d...)
	m.Labels = labelVal

	repos := v.AllowedRepositories
	if repos == nil {
		repos = []string{}
	}
	repoVal, d := types.ListValueFrom(ctx, types.StringType, repos)
	diags.Append(d...)
	m.AllowedRepositories = repoVal

	m.CreatedAt = types.StringValue(v.CreatedAt)
	m.UpdatedAt = types.StringValue(v.UpdatedAt)

	return diags
}
