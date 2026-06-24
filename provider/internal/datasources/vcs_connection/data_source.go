// Package vcs_connection implements the terrapod_vcs_connection data source.
// Migrated to go-terrapod (#347).
package vcs_connection

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var _ datasource.DataSource = &vcsConnectionDataSource{}

type vcsConnectionDataSource struct {
	tc *terrapod.Client
}

type vcsConnectionDataSourceModel struct {
	ID                   types.String `tfsdk:"id"`
	Name                 types.String `tfsdk:"name"`
	Provider             types.String `tfsdk:"vcs_provider"`
	ServerURL            types.String `tfsdk:"server_url"`
	Status               types.String `tfsdk:"status"`
	HasToken             types.Bool   `tfsdk:"has_token"`
	HasWebhookSecret     types.Bool   `tfsdk:"has_webhook_secret"`
	GithubAppID          types.Int64  `tfsdk:"github_app_id"`
	GithubInstallationID types.Int64  `tfsdk:"github_installation_id"`
	GithubAccountLogin   types.String `tfsdk:"github_account_login"`
	GithubAccountType    types.String `tfsdk:"github_account_type"`
	CreatedAt            types.String `tfsdk:"created_at"`
	UpdatedAt            types.String `tfsdk:"updated_at"`
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
			"id":                     schema.StringAttribute{Computed: true, Description: "VCS connection ID."},
			"name":                   schema.StringAttribute{Required: true, Description: "Connection name."},
			"vcs_provider":           schema.StringAttribute{Computed: true, Description: "VCS provider (github/gitlab)."},
			"server_url":             schema.StringAttribute{Computed: true, Description: "Server URL."},
			"status":                 schema.StringAttribute{Computed: true, Description: "Connection status."},
			"has_token":              schema.BoolAttribute{Computed: true, Description: "Whether a token is configured."},
			"has_webhook_secret":     schema.BoolAttribute{Computed: true, Description: "Whether a per-connection webhook secret is configured."},
			"github_app_id":          schema.Int64Attribute{Computed: true, Description: "GitHub App ID."},
			"github_installation_id": schema.Int64Attribute{Computed: true, Description: "GitHub installation ID."},
			"github_account_login":   schema.StringAttribute{Computed: true, Description: "GitHub account login."},
			"github_account_type":    schema.StringAttribute{Computed: true, Description: "GitHub account type."},
			"created_at":             schema.StringAttribute{Computed: true, Description: "Creation timestamp."},
			"updated_at":             schema.StringAttribute{Computed: true, Description: "Update timestamp."},
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
	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: c.BaseURL, Token: c.Token})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	d.tc = tc
}

func (d *vcsConnectionDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config vcsConnectionDataSourceModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	conns, err := d.tc.ListVCSConnections(ctx)
	if err != nil {
		resp.Diagnostics.AddError("Failed to list VCS connections", err.Error())
		return
	}

	name := config.Name.ValueString()
	for i := range conns {
		c := &conns[i]
		if c.Name != name {
			continue
		}
		config.ID = types.StringValue(c.ID)
		config.Name = types.StringValue(c.Name)
		config.Provider = types.StringValue(c.Provider)
		config.Status = types.StringValue(c.Status)
		config.HasToken = types.BoolValue(c.HasToken)
		config.HasWebhookSecret = types.BoolValue(c.HasWebhookSecret)
		config.CreatedAt = types.StringValue(c.CreatedAt)
		config.UpdatedAt = types.StringValue(c.UpdatedAt)

		if c.ServerURL != "" {
			config.ServerURL = types.StringValue(c.ServerURL)
		} else {
			config.ServerURL = types.StringNull()
		}
		if c.GithubAppID > 0 {
			config.GithubAppID = types.Int64Value(c.GithubAppID)
		} else {
			config.GithubAppID = types.Int64Null()
		}
		if c.GithubInstallationID > 0 {
			config.GithubInstallationID = types.Int64Value(c.GithubInstallationID)
		} else {
			config.GithubInstallationID = types.Int64Null()
		}
		if c.GithubAccountLogin != "" {
			config.GithubAccountLogin = types.StringValue(c.GithubAccountLogin)
		} else {
			config.GithubAccountLogin = types.StringNull()
		}
		if c.GithubAccountType != "" {
			config.GithubAccountType = types.StringValue(c.GithubAccountType)
		} else {
			config.GithubAccountType = types.StringNull()
		}

		resp.Diagnostics.Append(resp.State.Set(ctx, &config)...)
		return
	}

	resp.Diagnostics.AddError("VCS connection not found", fmt.Sprintf("No VCS connection named %q", name))
}
