// Package agent_pool implements the terrapod_agent_pool data source.
//
// API Contract: GET /api/v2/organizations/default/agent-pools
// Lists all pools, filters by name client-side.
package agent_pool

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var _ datasource.DataSource = &agentPoolDataSource{}

type agentPoolDataSource struct {
	client *client.Client
}

type agentPoolDataSourceModel struct {
	ID          types.String `tfsdk:"id"`
	Name        types.String `tfsdk:"name"`
	Description types.String `tfsdk:"description"`
	CreatedAt   types.String `tfsdk:"created_at"`
	UpdatedAt   types.String `tfsdk:"updated_at"`
}

func NewDataSource() datasource.DataSource {
	return &agentPoolDataSource{}
}

func (d *agentPoolDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_agent_pool"
}

func (d *agentPoolDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Look up a Terrapod agent pool by name.",
		Attributes: map[string]schema.Attribute{
			"id":          schema.StringAttribute{Computed: true, Description: "Agent pool ID."},
			"name":        schema.StringAttribute{Required: true, Description: "Agent pool name."},
			"description": schema.StringAttribute{Computed: true, Description: "Description."},
			"created_at":  schema.StringAttribute{Computed: true, Description: "Creation timestamp."},
			"updated_at":  schema.StringAttribute{Computed: true, Description: "Update timestamp."},
		},
	}
}

func (d *agentPoolDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
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

func (d *agentPoolDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config agentPoolDataSourceModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	data, err := d.client.Get(ctx, "/api/v2/organizations/default/agent-pools")
	if err != nil {
		resp.Diagnostics.AddError("Failed to list agent pools", err.Error())
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
			config.Description = types.StringValue(client.GetStringAttr(&r, "description"))
			config.CreatedAt = types.StringValue(client.GetStringAttr(&r, "created-at"))
			config.UpdatedAt = types.StringValue(client.GetStringAttr(&r, "updated-at"))
			resp.Diagnostics.Append(resp.State.Set(ctx, &config)...)
			return
		}
	}

	resp.Diagnostics.AddError("Agent pool not found", fmt.Sprintf("No agent pool named %q", name))
}
