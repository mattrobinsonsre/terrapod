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
	Engine     types.String            `tfsdk:"engine"`
	Workspaces []workspaceSummaryModel `tfsdk:"workspaces"`
}

type workspaceSummaryModel struct {
	ID   types.String `tfsdk:"id"`
	Name types.String `tfsdk:"name"`
	// Engine is which IaC system this workspace belongs to. Without it a
	// configuration iterating this list cannot tell a Pulumi workspace from a
	// Terraform one, and sweeps both into whatever it does next (#1911) —
	// `execution_mode` and `locked` are true of every engine and say nothing
	// about which one produced them.
	Engine        types.String `tfsdk:"engine"`
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
		Description: "List Terrapod workspaces with optional name and engine filters.",
		Attributes: map[string]schema.Attribute{
			"search": schema.StringAttribute{
				Description: "Substring to filter workspace names.",
				Optional:    true,
			},
			"engine": schema.StringAttribute{
				Description: "Return only workspaces on this execution engine — " +
					"\"terraform\" or \"pulumi\", or another engine the deployment " +
					"enables. Omit for every engine. Use this before feeding the list " +
					"to anything that assumes one engine's shape.",
				Optional: true,
			},
			"workspaces": schema.ListNestedAttribute{
				Description: "List of matching workspaces.",
				Computed:    true,
				NestedObject: schema.NestedAttributeObject{
					Attributes: map[string]schema.Attribute{
						"id":   schema.StringAttribute{Computed: true, Description: "Workspace ID."},
						"name": schema.StringAttribute{Computed: true, Description: "Workspace name."},
						"engine": schema.StringAttribute{
							Computed: true,
							Description: "The execution engine family this workspace belongs to — " +
								"\"terraform\" or \"pulumi\". Distinct from execution_mode.",
						},
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

	workspaces, err := listWorkspaces(ctx, d.tc, listOptions(config))
	if err != nil {
		resp.Diagnostics.AddError("Failed to list workspaces", err.Error())
		return
	}

	config.Workspaces = workspaces
	resp.Diagnostics.Append(resp.State.Set(ctx, &config)...)
}

// listOptions turns the configured filters into the SDK's list options.
//
// Both filters are SERVER-side: go-terrapod sends `search[name]` and
// `filter[engine]` as query parameters on the native workspace list. The
// client-side drop in listWorkspaces is a backstop behind this, not the
// mechanism — see wantEngine.
func listOptions(config workspacesDataSourceModel) terrapod.WorkspaceListOptions {
	opts := terrapod.WorkspaceListOptions{}
	if !config.Search.IsNull() {
		opts.Search = config.Search.ValueString()
	}
	if !config.Engine.IsNull() {
		opts.Engine = config.Engine.ValueString()
	}
	return opts
}

// listWorkspaces pages the whole list and maps it into the HCL shape.
//
// The data source historically returned everything matching the filter in one
// go (no pagination knobs in the HCL shape), so pagination is driven explicitly
// here rather than truncating at the server-default page size.
func listWorkspaces(ctx context.Context, tc *terrapod.Client, opts terrapod.WorkspaceListOptions) ([]workspaceSummaryModel, error) {
	workspaces := make([]workspaceSummaryModel, 0)
	opts.PageNumber = 1
	if opts.PageSize == 0 {
		opts.PageSize = 100
	}
	for {
		list, err := tc.ListWorkspaces(ctx, opts)
		if err != nil {
			return nil, err
		}
		for i := range list.Items {
			ws := &list.Items[i]
			if !wantEngine(opts.Engine, ws.Engine) {
				continue
			}
			workspaces = append(workspaces, workspaceSummaryModel{
				ID:            types.StringValue(ws.ID),
				Name:          types.StringValue(ws.Name),
				Engine:        types.StringValue(ws.Engine),
				ExecutionMode: types.StringValue(ws.ExecutionMode),
				Locked:        types.BoolValue(ws.Locked),
			})
		}
		if list.TotalPages == 0 || opts.PageNumber >= list.TotalPages {
			break
		}
		opts.PageNumber++
	}
	return workspaces, nil
}

// wantEngine reports whether a listed workspace belongs in an engine-filtered
// result. It is a BACKSTOP behind the server's own `filter[engine]`, not a
// replacement for it: an unrecognised query parameter is ignored rather than
// refused, so a server predating the filter would answer an engine-narrowed
// request with every engine — and the whole point of the filter is that the
// caller is about to treat the result as one engine's. A silently-ignored
// filter is the failure it exists to prevent, so the result is checked rather
// than assumed.
//
// A server that predates the `engine` attribute entirely reports "" for every
// workspace, which is Terraform: that is the value the column defaults to, and
// the only engine such a server has.
func wantEngine(want, got string) bool {
	if want == "" {
		return true
	}
	if got == "" {
		got = "terraform"
	}
	return got == want
}
