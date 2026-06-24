// Package role implements the terrapod_role data source.
// Migrated to go-terrapod (#347).
package role

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var _ datasource.DataSource = &roleDataSource{}

type roleDataSource struct {
	tc *terrapod.Client
}

type roleDataSourceModel struct {
	Name                types.String `tfsdk:"name"`
	Description         types.String `tfsdk:"description"`
	AllowLabels         types.Map    `tfsdk:"allow_labels"`
	AllowNames          types.List   `tfsdk:"allow_names"`
	DenyLabels          types.Map    `tfsdk:"deny_labels"`
	DenyNames           types.List   `tfsdk:"deny_names"`
	WorkspacePermission types.String `tfsdk:"workspace_permission"`
	PoolPermission      types.String `tfsdk:"pool_permission"`
	RegistryPermission  types.String `tfsdk:"registry_permission"`
	CatalogPermission   types.String `tfsdk:"catalog_permission"`
	BuiltIn             types.Bool   `tfsdk:"built_in"`
	CreatedAt           types.String `tfsdk:"created_at"`
	UpdatedAt           types.String `tfsdk:"updated_at"`
}

func NewDataSource() datasource.DataSource {
	return &roleDataSource{}
}

func (d *roleDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_role"
}

func (d *roleDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Look up a Terrapod role by name.",
		Attributes: map[string]schema.Attribute{
			"name":                 schema.StringAttribute{Required: true, Description: "Role name."},
			"description":          schema.StringAttribute{Computed: true, Description: "Description."},
			"allow_labels":         schema.MapAttribute{Computed: true, ElementType: types.StringType, Description: "Allow labels."},
			"allow_names":          schema.ListAttribute{Computed: true, ElementType: types.StringType, Description: "Allow name patterns."},
			"deny_labels":          schema.MapAttribute{Computed: true, ElementType: types.StringType, Description: "Deny labels."},
			"deny_names":           schema.ListAttribute{Computed: true, ElementType: types.StringType, Description: "Deny name patterns."},
			"workspace_permission": schema.StringAttribute{Computed: true, Description: "Permission level."},
			"pool_permission":      schema.StringAttribute{Computed: true, Description: "Agent pool permission level: read, write, or admin."},
			"registry_permission":  schema.StringAttribute{Computed: true, Description: "Registry (modules + providers) permission level: read, write, or admin."},
			"catalog_permission":   schema.StringAttribute{Computed: true, Description: "Service-catalog permission level: none, read, use, or admin."},
			"built_in":             schema.BoolAttribute{Computed: true, Description: "Whether the role is built-in."},
			"created_at":           schema.StringAttribute{Computed: true, Description: "Creation timestamp."},
			"updated_at":           schema.StringAttribute{Computed: true, Description: "Update timestamp."},
		},
	}
}

func (d *roleDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
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

func (d *roleDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config roleDataSourceModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	role, err := d.tc.GetRole(ctx, config.Name.ValueString())
	if err != nil {
		resp.Diagnostics.AddError("Failed to read role", err.Error())
		return
	}

	config.Name = types.StringValue(role.Name)
	if role.Description != "" {
		config.Description = types.StringValue(role.Description)
	} else {
		config.Description = types.StringNull()
	}
	config.WorkspacePermission = types.StringValue(role.WorkspacePermission)
	config.PoolPermission = types.StringValue(role.PoolPermission)
	config.RegistryPermission = types.StringValue(role.RegistryPermission)
	config.CatalogPermission = types.StringValue(role.CatalogPermission)
	config.BuiltIn = types.BoolValue(role.BuiltIn)
	config.CreatedAt = types.StringValue(role.CreatedAt)
	config.UpdatedAt = types.StringValue(role.UpdatedAt)

	if len(role.AllowLabels) > 0 {
		val, dl := types.MapValueFrom(ctx, types.StringType, role.AllowLabels)
		resp.Diagnostics.Append(dl...)
		config.AllowLabels = val
	} else {
		config.AllowLabels = types.MapNull(types.StringType)
	}
	if len(role.DenyLabels) > 0 {
		val, dl := types.MapValueFrom(ctx, types.StringType, role.DenyLabels)
		resp.Diagnostics.Append(dl...)
		config.DenyLabels = val
	} else {
		config.DenyLabels = types.MapNull(types.StringType)
	}
	if len(role.AllowNames) > 0 {
		val, dl := types.ListValueFrom(ctx, types.StringType, role.AllowNames)
		resp.Diagnostics.Append(dl...)
		config.AllowNames = val
	} else {
		config.AllowNames = types.ListNull(types.StringType)
	}
	if len(role.DenyNames) > 0 {
		val, dl := types.ListValueFrom(ctx, types.StringType, role.DenyNames)
		resp.Diagnostics.Append(dl...)
		config.DenyNames = val
	} else {
		config.DenyNames = types.ListNull(types.StringType)
	}

	resp.Diagnostics.Append(resp.State.Set(ctx, &config)...)
}
