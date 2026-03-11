// Package user implements the terrapod_user data source.
//
// API Contract: GET /api/v2/users/{email}
// Looks up a single user by email.
package user

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var _ datasource.DataSource = &userDataSource{}

type userDataSource struct {
	client *client.Client
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
	d.client = c
}

func (d *userDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config userDataSourceModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	data, err := d.client.Get(ctx, "/api/v2/users/"+config.Email.ValueString())
	if err != nil {
		resp.Diagnostics.AddError("Failed to read user", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	config.Email = types.StringValue(client.GetStringAttr(res, "email"))
	config.DisplayName = types.StringValue(client.GetStringAttr(res, "display-name"))
	config.IsActive = types.BoolValue(client.GetBoolAttr(res, "is-active"))
	config.HasPassword = types.BoolValue(client.GetBoolAttr(res, "has-password"))
	config.CreatedAt = types.StringValue(client.GetStringAttr(res, "created-at"))
	config.UpdatedAt = types.StringValue(client.GetStringAttr(res, "updated-at"))

	if v := client.GetStringAttr(res, "last-login-at"); v != "" {
		config.LastLoginAt = types.StringValue(v)
	} else {
		config.LastLoginAt = types.StringNull()
	}

	resp.Diagnostics.Append(resp.State.Set(ctx, &config)...)
}
