// Package storage_locations implements the terrapod_storage_locations data
// source (#2547): which named object stores this deployment has configured.
//
// A workspace's and an autodiscovery rule's `storage_location` must name one of
// them, and the server refuses a name it has not been configured with. The set
// is deployment configuration — `api.config.storage.locations` in the chart —
// so there is no other way for a configuration to discover it: without this,
// a practitioner's only routes are the UI, an MCP tool, or applying and reading
// the 422. Both resources' schema descriptions point here.
//
// # an empty list is the ordinary answer
//
// A deployment that configures no named locations has exactly one object store,
// which is the default configuration. `locations` is then empty and the right
// thing for a configuration to do is leave `storage_location` unset. So a
// `contains()` check against this must treat empty as "nothing to choose"
// rather than as a failure, and an empty list is returned rather than null
// precisely so that check does not error.
//
// # `default` is not in it
//
// The deployment's main store is the ABSENCE of a named location, not a
// location called `default`. The server reserves that name and refuses it as a
// value, so it is not listed — a configuration selecting "the deployment's own
// store" omits the attribute or sets null.
//
// # names and backends only
//
// A location's bucket, region, endpoint and credentials are deployment
// configuration and are deliberately not served: the API returns the name and
// the backend kind, which is all a configuration needs to choose one, and the
// credentials block sits directly beside them in the config model.
package storage_locations

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var _ datasource.DataSource = &locationsDataSource{}

type locationsDataSource struct {
	tc *terrapod.Client
}

type locationsModel struct {
	Locations types.List `tfsdk:"locations"`
	Names     types.List `tfsdk:"names"`
}

// NewDataSource returns a new storage-locations data source.
func NewDataSource() datasource.DataSource {
	return &locationsDataSource{}
}

func (d *locationsDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_storage_locations"
}

func (d *locationsDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "The named object stores this deployment has configured, for " +
			"`terrapod_workspace`'s and `terrapod_autodiscovery_rule`'s " +
			"`storage_location` to select from. The set is deployment configuration, so " +
			"this is the only way a configuration can discover it. An EMPTY list is the " +
			"ordinary answer — it means one object store, which is the default " +
			"configuration — so treat empty as “nothing to choose” rather than as a " +
			"failure. The deployment's own store is the absence of a named location and " +
			"is not listed.",
		Attributes: map[string]schema.Attribute{
			"locations": schema.ListNestedAttribute{
				Computed:    true,
				Description: "Every configured location, in the order the server returns them.",
				NestedObject: schema.NestedAttributeObject{
					Attributes: map[string]schema.Attribute{
						"name": schema.StringAttribute{
							Computed: true,
							Description: "The value `storage_location` would be set to. Never " +
								"`default`, which names the absence of a location.",
						},
						"backend": schema.StringAttribute{
							Computed: true,
							Description: "The store's kind: `s3`, `azure` or `gcs`. Never " +
								"`filesystem` — a named location cannot be one.",
						},
					},
				},
			},
			"names": schema.ListAttribute{
				Computed:    true,
				ElementType: types.StringType,
				Description: "Just the names, for a `contains()` check or a `for_each`.",
			},
		},
	}
}

func (d *locationsDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	c, ok := req.ProviderData.(*client.Client)
	if !ok {
		resp.Diagnostics.AddError(
			"Unexpected provider data type",
			fmt.Sprintf("Expected *client.Client, got %T", req.ProviderData),
		)
		return
	}
	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: c.BaseURL, Token: c.Token})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	d.tc = tc
}

func (d *locationsDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config locationsModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	found, err := d.tc.ListStorageLocations(ctx)
	if err != nil {
		resp.Diagnostics.AddError("Failed to read storage locations", err.Error())
		return
	}

	objType := types.ObjectType{AttrTypes: map[string]attr.Type{
		"name":    types.StringType,
		"backend": types.StringType,
	}}

	objs := make([]attr.Value, 0, len(found))
	names := make([]attr.Value, 0, len(found))
	for _, l := range found {
		obj, diags := types.ObjectValue(objType.AttrTypes, map[string]attr.Value{
			"name":    types.StringValue(l.Name),
			"backend": types.StringValue(l.Backend),
		})
		resp.Diagnostics.Append(diags...)
		if resp.Diagnostics.HasError() {
			return
		}
		objs = append(objs, obj)
		names = append(names, types.StringValue(l.Name))
	}

	// Empty lists, never null: a deployment with no named locations is the
	// ordinary case, and `contains(data.terrapod_storage_locations.this.names,
	// var.loc)` on a null collection errors instead of answering false.
	locList, diags := types.ListValue(objType, objs)
	resp.Diagnostics.Append(diags...)
	nameList, diags := types.ListValue(types.StringType, names)
	resp.Diagnostics.Append(diags...)
	if resp.Diagnostics.HasError() {
		return
	}

	config.Locations = locList
	config.Names = nameList

	resp.Diagnostics.Append(resp.State.Set(ctx, &config)...)
}
