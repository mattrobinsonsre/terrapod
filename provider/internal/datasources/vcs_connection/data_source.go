// Package vcs_connection implements the terrapod_vcs_connection data source.
//
// API Contract: GET /api/v2/organizations/default/vcs-connections
// Lists all connections, filters by name client-side.
package vcs_connection

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var _ datasource.DataSource = &vcsConnectionDataSource{}

type vcsConnectionDataSource struct {
	client *client.Client
}

type vcsConnectionDataSourceModel struct {
	ID                    types.String `tfsdk:"id"`
	Name                  types.String `tfsdk:"name"`
	Provider              types.String `tfsdk:"vcs_provider"`
	ServerURL             types.String `tfsdk:"server_url"`
	Status                types.String `tfsdk:"status"`
	HasToken              types.Bool   `tfsdk:"has_token"`
	GithubAppID           types.Int64  `tfsdk:"github_app_id"`
	GithubInstallationID  types.Int64  `tfsdk:"github_installation_id"`
	GithubAccountLogin    types.String `tfsdk:"github_account_login"`
	GithubAccountType     types.String `tfsdk:"github_account_type"`
	CreatedAt             types.String `tfsdk:"created_at"`
	UpdatedAt             types.String `tfsdk:"updated_at"`
}

func NewDataSource() datasource.DataSource {
	return &vcsConnectionDataSource{}
}

func (d *vcsConnectionDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_vcs_connection"
}

func (d *vcsConnectionDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Look up a Terrapod VCS connection by name.",
		Attributes: map[string]schema.Attribute{
			"id":                      schema.StringAttribute{Computed: true, Description: "VCS connection ID."},
			"name":                    schema.StringAttribute{Required: true, Description: "Connection name."},
			"vcs_provider":            schema.StringAttribute{Computed: true, Description: "VCS provider (github/gitlab)."},
			"server_url":              schema.StringAttribute{Computed: true, Description: "Server URL."},
			"status":                  schema.StringAttribute{Computed: true, Description: "Connection status."},
			"has_token":               schema.BoolAttribute{Computed: true, Description: "Whether a token is configured."},
			"github_app_id":           schema.Int64Attribute{Computed: true, Description: "GitHub App ID."},
			"github_installation_id":  schema.Int64Attribute{Computed: true, Description: "GitHub installation ID."},
			"github_account_login":    schema.StringAttribute{Computed: true, Description: "GitHub account login."},
			"github_account_type":     schema.StringAttribute{Computed: true, Description: "GitHub account type."},
			"created_at":              schema.StringAttribute{Computed: true, Description: "Creation timestamp."},
			"updated_at":              schema.StringAttribute{Computed: true, Description: "Update timestamp."},
		},
	}
}

func (d *vcsConnectionDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	c, ok := req.ProviderData.(*client.Client)
	if !ok {
		resp.Diagnostics.AddError("Unexpected provider data type", fmt.Sprintf("Expected *client.Client, got %T", req.ProviderData))
		return
	}
	d.client = c
}

func (d *vcsConnectionDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config vcsConnectionDataSourceModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	data, err := d.client.Get(ctx, "/api/v2/organizations/default/vcs-connections")
	if err != nil {
		resp.Diagnostics.AddError("Failed to list VCS connections", err.Error())
		return
	}

	resources, err := client.ParseResourceList(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	name := config.Name.ValueString()
	for _, r := range resources {
		if client.GetStringAttr(&r, "name") == name {
			config.ID = types.StringValue(r.ID)
			config.Name = types.StringValue(client.GetStringAttr(&r, "name"))
			config.Provider = types.StringValue(client.GetStringAttr(&r, "provider"))
			config.Status = types.StringValue(client.GetStringAttr(&r, "status"))
			config.HasToken = types.BoolValue(client.GetBoolAttr(&r, "has-token"))
			config.CreatedAt = types.StringValue(client.GetStringAttr(&r, "created-at"))
			config.UpdatedAt = types.StringValue(client.GetStringAttr(&r, "updated-at"))

			if v := client.GetStringAttr(&r, "server-url"); v != "" {
				config.ServerURL = types.StringValue(v)
			} else {
				config.ServerURL = types.StringNull()
			}
			if v := client.GetIntAttr(&r, "github-app-id"); v > 0 {
				config.GithubAppID = types.Int64Value(v)
			} else {
				config.GithubAppID = types.Int64Null()
			}
			if v := client.GetIntAttr(&r, "github-installation-id"); v > 0 {
				config.GithubInstallationID = types.Int64Value(v)
			} else {
				config.GithubInstallationID = types.Int64Null()
			}
			if v := client.GetStringAttr(&r, "github-account-login"); v != "" {
				config.GithubAccountLogin = types.StringValue(v)
			} else {
				config.GithubAccountLogin = types.StringNull()
			}
			if v := client.GetStringAttr(&r, "github-account-type"); v != "" {
				config.GithubAccountType = types.StringValue(v)
			} else {
				config.GithubAccountType = types.StringNull()
			}

			resp.Diagnostics.Append(resp.State.Set(ctx, &config)...)
			return
		}
	}

	resp.Diagnostics.AddError("VCS connection not found", fmt.Sprintf("No VCS connection named %q", name))
}
