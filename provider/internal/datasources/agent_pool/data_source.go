// Package agent_pool implements the terrapod_agent_pool data source.
// Migrated to go-terrapod (#347).
package agent_pool

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var _ datasource.DataSource = &agentPoolDataSource{}

type agentPoolDataSource struct {
	tc *terrapod.Client
}

type agentPoolDataSourceModel struct {
	ID          types.String `tfsdk:"id"`
	Name        types.String `tfsdk:"name"`
	Description types.String `tfsdk:"description"`
	Labels      types.Map    `tfsdk:"labels"`
	OwnerEmail  types.String `tfsdk:"owner_email"`
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
			"labels":      schema.MapAttribute{Computed: true, ElementType: types.StringType, Description: "Pool labels for RBAC."},
			"owner_email": schema.StringAttribute{Computed: true, Description: "Pool owner email."},
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
	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: c.BaseURL, Token: c.Token})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	d.tc = tc
}

func (d *agentPoolDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config agentPoolDataSourceModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	pools, err := d.tc.ListAgentPools(ctx)
	if err != nil {
		resp.Diagnostics.AddError("Failed to list agent pools", err.Error())
		return
	}

	name := config.Name.ValueString()
	for i := range pools {
		p := &pools[i]
		if p.Name != name {
			continue
		}
		config.ID = types.StringValue(p.ID)
		config.Name = types.StringValue(p.Name)
		config.Description = types.StringValue(p.Description)

		if len(p.Labels) > 0 {
			val, dl := types.MapValueFrom(ctx, types.StringType, p.Labels)
			resp.Diagnostics.Append(dl...)
			config.Labels = val
		} else {
			config.Labels = types.MapNull(types.StringType)
		}

		if p.OwnerEmail != "" {
			config.OwnerEmail = types.StringValue(p.OwnerEmail)
		} else {
			config.OwnerEmail = types.StringNull()
		}

		config.CreatedAt = types.StringValue(p.CreatedAt)
		config.UpdatedAt = types.StringValue(p.UpdatedAt)
		resp.Diagnostics.Append(resp.State.Set(ctx, &config)...)
		return
	}

	resp.Diagnostics.AddError("Agent pool not found", fmt.Sprintf("No agent pool named %q", name))
}
