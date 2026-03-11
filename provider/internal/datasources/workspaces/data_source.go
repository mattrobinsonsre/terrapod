// Package workspaces implements the terrapod_workspaces data source (list).
//
// API Contract: GET /api/v2/organizations/default/workspaces?search[name]={search}
// Returns a filtered list of workspaces. Supports name substring search.
package workspaces

import (
	"context"
	"fmt"
	"net/url"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var _ datasource.DataSource = &workspacesDataSource{}

type workspacesDataSource struct {
	client *client.Client
}

type workspacesDataSourceModel struct {
	Search     types.String             `tfsdk:"search"`
	Workspaces []workspaceSummaryModel  `tfsdk:"workspaces"`
}

type workspaceSummaryModel struct {
	ID            types.String `tfsdk:"id"`
	Name          types.String `tfsdk:"name"`
	ExecutionMode types.String `tfsdk:"execution_mode"`
	Locked        types.Bool   `tfsdk:"locked"`
}

// NewDataSource returns a new workspaces list data source.
func NewDataSource() datasource.DataSource {
	return &workspacesDataSource{}
}

func (d *workspacesDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_workspaces"
}

func (d *workspacesDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "List Terrapod workspaces with optional name search.",
		Attributes: map[string]schema.Attribute{
			"search": schema.StringAttribute{
				Description: "Substring to filter workspace names.",
				Optional:    true,
			},
			"workspaces": schema.ListNestedAttribute{
				Description: "List of matching workspaces.",
				Computed:    true,
				NestedObject: schema.NestedAttributeObject{
					Attributes: map[string]schema.Attribute{
						"id":             schema.StringAttribute{Computed: true, Description: "Workspace ID."},
						"name":           schema.StringAttribute{Computed: true, Description: "Workspace name."},
						"execution_mode": schema.StringAttribute{Computed: true, Description: "Execution mode."},
						"locked":         schema.BoolAttribute{Computed: true, Description: "Lock status."},
					},
				},
			},
		},
	}
}

func (d *workspacesDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
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

func (d *workspacesDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config workspacesDataSourceModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	path := "/api/v2/organizations/default/workspaces"
	if !config.Search.IsNull() && config.Search.ValueString() != "" {
		path += "?search[name]=" + url.QueryEscape(config.Search.ValueString())
	}

	data, err := d.client.Get(ctx, path)
	if err != nil {
		resp.Diagnostics.AddError("Failed to list workspaces", err.Error())
		return
	}

	resources, err := client.ParseResourceList(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	workspaces := make([]workspaceSummaryModel, 0, len(resources))
	for _, r := range resources {
		workspaces = append(workspaces, workspaceSummaryModel{
			ID:            types.StringValue(r.ID),
			Name:          types.StringValue(client.GetStringAttr(&r, "name")),
			ExecutionMode: types.StringValue(client.GetStringAttr(&r, "execution-mode")),
			Locked:        types.BoolValue(client.GetBoolAttr(&r, "locked")),
		})
	}

	config.Workspaces = workspaces
	resp.Diagnostics.Append(resp.State.Set(ctx, &config)...)
}
