// Package catalog_instances implements the terrapod_catalog_instances
// data source (service catalog, #535). It lists the provisioned instances
// of a catalog item, optionally filtered client-side by name substring.
package catalog_instances

import (
	"context"
	"fmt"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var _ datasource.DataSource = &catalogInstancesDataSource{}

type catalogInstancesDataSource struct {
	tc *terrapod.Client
}

type catalogInstancesModel struct {
	CatalogItemID types.String           `tfsdk:"catalog_item_id"`
	NameContains  types.String           `tfsdk:"name_contains"`
	Instances     []catalogInstanceEntry `tfsdk:"instances"`
}

type catalogInstanceEntry struct {
	ID          types.String `tfsdk:"id"`
	Name        types.String `tfsdk:"name"`
	AgentPoolID types.String `tfsdk:"agent_pool_id"`
	VersionPin  types.String `tfsdk:"version_pin"`
}

// NewDataSource returns a new catalog instances data source.
func NewDataSource() datasource.DataSource {
	return &catalogInstancesDataSource{}
}

func (d *catalogInstancesDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_catalog_instances"
}

func (d *catalogInstancesDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Lists the provisioned instances of a Terrapod service-catalog item.",
		Attributes: map[string]schema.Attribute{
			"catalog_item_id": schema.StringAttribute{
				Required:    true,
				Description: "The catalog item whose instances to list.",
			},
			"name_contains": schema.StringAttribute{
				Optional:    true,
				Description: "Optional case-insensitive substring filter on instance name.",
			},
			"instances": schema.ListNestedAttribute{
				Computed:    true,
				Description: "The matching provisioned instances.",
				NestedObject: schema.NestedAttributeObject{
					Attributes: map[string]schema.Attribute{
						"id":            schema.StringAttribute{Computed: true, Description: "Instance (workspace) ID."},
						"name":          schema.StringAttribute{Computed: true, Description: "Instance name."},
						"agent_pool_id": schema.StringAttribute{Computed: true, Description: "Agent pool the instance runs on."},
						"version_pin":   schema.StringAttribute{Computed: true, Description: "Module version pin."},
					},
				},
			},
		},
	}
}

func (d *catalogInstancesDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
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

func (d *catalogInstancesDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config catalogInstancesModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	instances, err := d.tc.ListCatalogInstances(ctx, config.CatalogItemID.ValueString())
	if err != nil {
		resp.Diagnostics.AddError("Failed to list catalog instances", err.Error())
		return
	}

	var filter string
	if !config.NameContains.IsNull() && !config.NameContains.IsUnknown() {
		filter = strings.ToLower(config.NameContains.ValueString())
	}

	config.Instances = nil
	for i := range instances {
		inst := &instances[i]
		name := attrString(inst.Attributes, "name")
		if filter != "" && !strings.Contains(strings.ToLower(name), filter) {
			continue
		}
		config.Instances = append(config.Instances, catalogInstanceEntry{
			ID:          types.StringValue(inst.ID),
			Name:        types.StringValue(name),
			AgentPoolID: types.StringValue(attrString(inst.Attributes, "agent-pool-id")),
			VersionPin:  types.StringValue(attrString(inst.Attributes, "catalog-version-pin")),
		})
	}

	resp.Diagnostics.Append(resp.State.Set(ctx, &config)...)
}

// attrString reads a string attribute out of an SDK Attributes map,
// returning "" when absent or not a string.
func attrString(attrs map[string]any, key string) string {
	if v, ok := attrs[key]; ok && v != nil {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}
