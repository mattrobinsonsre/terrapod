// Package user implements the terrapod_user data source.
// Migrated to go-terrapod (#347).
package user

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var _ datasource.DataSource = &userDataSource{}

type userDataSource struct {
	tc *terrapod.Client
}

type userDataSourceModel struct {
	Email       types.String `tfsdk:"email"`
	DisplayName types.String `tfsdk:"display_name"`
	IsActive    types.Bool   `tfsdk:"is_active"`
	HasPassword types.Bool   `tfsdk:"has_password"`
	LastLoginAt types.String `tfsdk:"last_login_at"`
	CreatedAt   types.String `tfsdk:"created_at"`
	UpdatedAt   types.String `tfsdk:"updated_at"`
}

func NewDataSource() datasource.DataSource {
	return &userDataSource{}
}

func (d *userDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_user"
}

func (d *userDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Look up a Terrapod user by email.",
		Attributes: map[string]schema.Attribute{
			"email":        schema.StringAttribute{Required: true, Description: "User email."},
			"display_name": schema.StringAttribute{Computed: true, Description: "Display name."},
			"is_active":    schema.BoolAttribute{Computed: true, Description: "Whether the user is active."},
			"has_password": schema.BoolAttribute{Computed: true, Description: "Whether the user has a local password."},
			"last_login_at": schema.StringAttribute{Computed: true, Description: "Last login timestamp."},
			"created_at":   schema.StringAttribute{Computed: true, Description: "Creation timestamp."},
			"updated_at":   schema.StringAttribute{Computed: true, Description: "Update timestamp."},
		},
	}
}

func (d *userDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
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

func (d *userDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config userDataSourceModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	u, err := d.tc.GetUser(ctx, config.Email.ValueString())
	if err != nil {
		resp.Diagnostics.AddError("Failed to read user", err.Error())
		return
	}

	config.Email = types.StringValue(u.Email)
	config.DisplayName = types.StringValue(u.DisplayName)
	config.IsActive = types.BoolValue(u.IsActive)
	config.HasPassword = types.BoolValue(u.HasPassword)
	config.CreatedAt = types.StringValue(u.CreatedAt)
	config.UpdatedAt = types.StringValue(u.UpdatedAt)

	if u.LastLoginAt != "" {
		config.LastLoginAt = types.StringValue(u.LastLoginAt)
	} else {
		config.LastLoginAt = types.StringNull()
	}

	resp.Diagnostics.Append(resp.State.Set(ctx, &config)...)
}
