// Package role implements the terrapod_role data source.
//
// API Contract: GET /api/v2/roles/{name}
// Looks up a single role by name.
package role

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var _ datasource.DataSource = &roleDataSource{}

type roleDataSource struct {
	client *client.Client
}

type roleDataSourceModel struct {
	Name                types.String `tfsdk:"name"`
	Description         types.String `tfsdk:"description"`
	AllowLabels         types.Map    `tfsdk:"allow_labels"`
	AllowNames          types.List   `tfsdk:"allow_names"`
	DenyLabels          types.Map    `tfsdk:"deny_labels"`
	DenyNames           types.List   `tfsdk:"deny_names"`
	WorkspacePermission types.String `tfsdk:"workspace_permission"`
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
			"description":         schema.StringAttribute{Computed: true, Description: "Description."},
			"allow_labels":        schema.MapAttribute{Computed: true, ElementType: types.StringType, Description: "Allow labels."},
			"allow_names":         schema.ListAttribute{Computed: true, ElementType: types.StringType, Description: "Allow name patterns."},
			"deny_labels":         schema.MapAttribute{Computed: true, ElementType: types.StringType, Description: "Deny labels."},
			"deny_names":          schema.ListAttribute{Computed: true, ElementType: types.StringType, Description: "Deny name patterns."},
			"workspace_permission": schema.StringAttribute{Computed: true, Description: "Permission level."},
			"built_in":            schema.BoolAttribute{Computed: true, Description: "Whether the role is built-in."},
			"created_at":          schema.StringAttribute{Computed: true, Description: "Creation timestamp."},
			"updated_at":          schema.StringAttribute{Computed: true, Description: "Update timestamp."},
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
	d.client = c
}

func (d *roleDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config roleDataSourceModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	data, err := d.client.Get(ctx, "/api/v2/roles/"+config.Name.ValueString())
	if err != nil {
		resp.Diagnostics.AddError("Failed to read role", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	config.Name = types.StringValue(client.GetStringAttr(res, "name"))
	config.Description = types.StringValue(client.GetStringAttr(res, "description"))
	config.WorkspacePermission = types.StringValue(client.GetStringAttr(res, "workspace-permission"))
	config.BuiltIn = types.BoolValue(client.GetBoolAttr(res, "built-in"))
	config.CreatedAt = types.StringValue(client.GetStringAttr(res, "created-at"))
	config.UpdatedAt = types.StringValue(client.GetStringAttr(res, "updated-at"))

	if labels := client.GetMapAttr(res, "allow-labels"); len(labels) > 0 {
		config.AllowLabels, _ = types.MapValueFrom(ctx, types.StringType, labels)
	} else {
		config.AllowLabels = types.MapNull(types.StringType)
	}
	if labels := client.GetMapAttr(res, "deny-labels"); len(labels) > 0 {
		config.DenyLabels, _ = types.MapValueFrom(ctx, types.StringType, labels)
	} else {
		config.DenyLabels = types.MapNull(types.StringType)
	}
	if names := client.GetListAttr(res, "allow-names"); len(names) > 0 {
		config.AllowNames, _ = types.ListValueFrom(ctx, types.StringType, names)
	} else {
		config.AllowNames = types.ListNull(types.StringType)
	}
	if names := client.GetListAttr(res, "deny-names"); len(names) > 0 {
		config.DenyNames, _ = types.ListValueFrom(ctx, types.StringType, names)
	} else {
		config.DenyNames = types.ListNull(types.StringType)
	}

	resp.Diagnostics.Append(resp.State.Set(ctx, &config)...)
}
