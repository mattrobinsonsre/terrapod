// Package workspaces implements the terrapod_workspaces data source (list).
// Migrated to go-terrapod (#347).
package workspaces

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var _ datasource.DataSource = &workspacesDataSource{}

type workspacesDataSource struct {
	tc *terrapod.Client
}

type workspacesDataSourceModel struct {
	Search     types.String            `tfsdk:"search"`
	Workspaces []workspaceSummaryModel `tfsdk:"workspaces"`
}

type workspaceSummaryModel struct {
	ID            types.String `tfsdk:"id"`
	Name          types.String `tfsdk:"name"`
	ExecutionMode types.String `tfsdk:"execution_mode"`
	Locked        types.Bool   `tfsdk:"locked"`
}

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
	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: c.BaseURL, Token: c.Token})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	d.tc = tc
}

func (d *workspacesDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config workspacesDataSourceModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	opts := terrapod.WorkspaceListOptions{}
	if !config.Search.IsNull() {
		opts.Search = config.Search.ValueString()
	}

	// The data source historically returned everything matching the
	// filter in one go (no pagination knobs in the HCL shape). Drive
	// pagination explicitly so a large org doesn't truncate at the
	// server-default page size.
	workspaces := make([]workspaceSummaryModel, 0)
	opts.PageNumber = 1
	if opts.PageSize == 0 {
		opts.PageSize = 100
	}
	for {
		list, err := d.tc.ListWorkspaces(ctx, opts)
		if err != nil {
			resp.Diagnostics.AddError("Failed to list workspaces", err.Error())
			return
		}
		for i := range list.Items {
			ws := &list.Items[i]
			workspaces = append(workspaces, workspaceSummaryModel{
				ID:            types.StringValue(ws.ID),
				Name:          types.StringValue(ws.Name),
				ExecutionMode: types.StringValue(ws.ExecutionMode),
				Locked:        types.BoolValue(ws.Locked),
			})
		}
		if list.TotalPages == 0 || opts.PageNumber >= list.TotalPages {
			break
		}
		opts.PageNumber++
	}

	config.Workspaces = workspaces
	resp.Diagnostics.Append(resp.State.Set(ctx, &config)...)
}
