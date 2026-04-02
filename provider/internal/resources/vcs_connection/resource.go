// Package vcs_connection implements the terrapod_vcs_connection resource.
//
// API Contract (Terrapod API <-> Terraform Provider):
//
//	JSON:API type: "vcs-connections"
//	ID prefix: "vcs-"
//	Create:  POST   /api/v2/organizations/default/vcs-connections
//	Read:    GET    /api/v2/vcs-connections/{id}
//	Delete:  DELETE /api/v2/vcs-connections/{id}
//
// This resource is immutable — there is no update (PATCH) endpoint.
// Any attribute change forces replacement.
//
// Attribute mapping (JSON:API attribute -> Terraform schema attribute):
//
//	"name"                     -> name                     (string, required, forces new)
//	"provider"                 -> provider                 (string, required, forces new: "github" or "gitlab")
//	"server-url"               -> server_url               (string, optional, forces new)
//	"github-app-id"            -> github_app_id            (int,    optional, forces new)
//	"github-installation-id"   -> github_installation_id   (int,    optional, forces new)
//	"private-key"              -> private_key              (string, optional, sensitive, write-only, forces new)
//	"token"                    -> token                    (string, optional, sensitive, write-only, forces new)
//
// Read-only attributes:
//
//	"status"                   -> status                   (string, computed)
//	"has-token"                -> has_token                (bool,   computed)
//	"github-account-login"     -> github_account_login     (string, computed)
//	"github-account-type"      -> github_account_type      (string, computed)
//	"created-at"               -> created_at               (string, computed)
//	"updated-at"               -> updated_at               (string, computed)
//
// Import: by VCS connection ID (e.g. "vcs-abc123").
// Note: private_key and token values will not be available after import.
package vcs_connection

import (
	"context"
	"fmt"

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
	_ resource.Resource                = &vcsConnectionResource{}
	_ resource.ResourceWithImportState = &vcsConnectionResource{}
)

// vcsConnectionModel maps the Terraform schema to Go types.
type vcsConnectionModel struct {
	ID types.String `tfsdk:"id"`

	// Writable attributes (all force replacement — resource is immutable)
	Name                 types.String `tfsdk:"name"`
	Provider             types.String `tfsdk:"vcs_provider"`
	ServerURL            types.String `tfsdk:"server_url"`
	GithubAppID          types.Int64  `tfsdk:"github_app_id"`
	GithubInstallationID types.Int64  `tfsdk:"github_installation_id"`
	PrivateKey           types.String `tfsdk:"private_key"`
	Token                types.String `tfsdk:"token"`

	// Read-only attributes
	Status             types.String `tfsdk:"status"`
	HasToken           types.Bool   `tfsdk:"has_token"`
	GithubAccountLogin types.String `tfsdk:"github_account_login"`
	GithubAccountType  types.String `tfsdk:"github_account_type"`
	CreatedAt          types.String `tfsdk:"created_at"`
	UpdatedAt          types.String `tfsdk:"updated_at"`
}

type vcsConnectionResource struct {
	client *client.Client
}

// NewResource returns a new VCS connection resource.
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

			// Read-only
			"status": schema.StringAttribute{
				Description: "The connection status.",
				Computed:    true,
			},
			"has_token": schema.BoolAttribute{
				Description: "Whether the connection has a token/key configured.",
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
}

func (r *vcsConnectionResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan vcsConnectionModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs := buildVCSConnectionAttrs(&plan)

	body, err := client.MarshalResource("vcs-connections", attrs, nil)
	if err != nil {
		resp.Diagnostics.AddError("Failed to marshal request", err.Error())
		return
	}

	data, err := r.client.Post(ctx, "/api/v2/organizations/default/vcs-connections", body)
	if err != nil {
		resp.Diagnostics.AddError("Failed to create VCS connection", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	readVCSConnectionIntoModel(res, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *vcsConnectionResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state vcsConnectionModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	data, err := r.client.Get(ctx, "/api/v2/vcs-connections/"+state.ID.ValueString())
	if err != nil {
		if client.IsNotFound(err) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Failed to read VCS connection", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	// Preserve write-only fields from state (API never returns them).
	privateKey := state.PrivateKey
	token := state.Token

	readVCSConnectionIntoModel(res, &state)
	state.PrivateKey = privateKey
	state.Token = token
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

// Update is not supported — all attributes force replacement.
// This method is required by the Resource interface but should never be called.
func (r *vcsConnectionResource) Update(_ context.Context, _ resource.UpdateRequest, resp *resource.UpdateResponse) {
	resp.Diagnostics.AddError(
		"Update not supported",
		"VCS connections are immutable. All changes force replacement.",
	)
}

func (r *vcsConnectionResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state vcsConnectionModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	err := r.client.Delete(ctx, "/api/v2/vcs-connections/"+state.ID.ValueString())
	if err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Failed to delete VCS connection", err.Error())
	}
}

func (r *vcsConnectionResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

// buildVCSConnectionAttrs converts the Terraform model into JSON:API attributes.
func buildVCSConnectionAttrs(m *vcsConnectionModel) map[string]any {
	attrs := map[string]any{
		"name":     m.Name.ValueString(),
		"provider": m.Provider.ValueString(),
	}

	if !m.ServerURL.IsNull() && !m.ServerURL.IsUnknown() {
		attrs["server-url"] = m.ServerURL.ValueString()
	}
	if !m.GithubAppID.IsNull() && !m.GithubAppID.IsUnknown() {
		attrs["github-app-id"] = m.GithubAppID.ValueInt64()
	}
	if !m.GithubInstallationID.IsNull() && !m.GithubInstallationID.IsUnknown() {
		attrs["github-installation-id"] = m.GithubInstallationID.ValueInt64()
	}
	if !m.PrivateKey.IsNull() && !m.PrivateKey.IsUnknown() {
		attrs["private-key"] = m.PrivateKey.ValueString()
	}
	if !m.Token.IsNull() && !m.Token.IsUnknown() {
		attrs["token"] = m.Token.ValueString()
	}

	return attrs
}

// readVCSConnectionIntoModel populates the Terraform model from a JSON:API resource.
func readVCSConnectionIntoModel(res *client.Resource, m *vcsConnectionModel) {
	m.ID = types.StringValue(res.ID)
	m.Name = types.StringValue(client.GetStringAttr(res, "name"))
	m.Provider = types.StringValue(client.GetStringAttr(res, "provider"))

	if v := client.GetStringAttr(res, "server-url"); v != "" {
		m.ServerURL = types.StringValue(v)
	} else {
		m.ServerURL = types.StringNull()
	}

	if v := client.GetIntAttr(res, "github-app-id"); v != 0 {
		m.GithubAppID = types.Int64Value(v)
	} else {
		m.GithubAppID = types.Int64Null()
	}
	if v := client.GetIntAttr(res, "github-installation-id"); v != 0 {
		m.GithubInstallationID = types.Int64Value(v)
	} else {
		m.GithubInstallationID = types.Int64Null()
	}

	// private_key and token are write-only — caller preserves them from state.

	m.Status = types.StringValue(client.GetStringAttr(res, "status"))
	m.HasToken = types.BoolValue(client.GetBoolAttr(res, "has-token"))

	if v := client.GetStringAttr(res, "github-account-login"); v != "" {
		m.GithubAccountLogin = types.StringValue(v)
	} else {
		m.GithubAccountLogin = types.StringNull()
	}
	if v := client.GetStringAttr(res, "github-account-type"); v != "" {
		m.GithubAccountType = types.StringValue(v)
	} else {
		m.GithubAccountType = types.StringNull()
	}

	m.CreatedAt = types.StringValue(client.GetStringAttr(res, "created-at"))
	m.UpdatedAt = types.StringValue(client.GetStringAttr(res, "updated-at"))
}
