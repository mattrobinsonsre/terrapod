// Package catalog_item_interface implements the
// terrapod_catalog_item_interface data source (#1585): the inputs and outputs
// of the module version a service-catalog item resolves to — its pin, or the
// latest uploaded version. Derived from the module registry, never stored on
// the item.
package catalog_item_interface

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var _ datasource.DataSource = &catalogItemInterfaceDataSource{}

type catalogItemInterfaceDataSource struct {
	tc *terrapod.Client
}

type catalogItemInterfaceModel struct {
	CatalogItemID   types.String  `tfsdk:"catalog_item_id"`
	ResolvedVersion types.String  `tfsdk:"resolved_version"`
	Inputs          []inputEntry  `tfsdk:"inputs"`
	Outputs         []outputEntry `tfsdk:"outputs"`
	InterfaceError  types.String  `tfsdk:"interface_error"`
}

type inputEntry struct {
	Name        types.String `tfsdk:"name"`
	Type        types.String `tfsdk:"type"`
	Description types.String `tfsdk:"description"`
	Default     types.String `tfsdk:"default"`
	Required    types.Bool   `tfsdk:"required"`
	Sensitive   types.Bool   `tfsdk:"sensitive"`
}

type outputEntry struct {
	Name        types.String `tfsdk:"name"`
	Description types.String `tfsdk:"description"`
	Sensitive   types.Bool   `tfsdk:"sensitive"`
}

// NewDataSource returns a new catalog item interface data source.
func NewDataSource() datasource.DataSource {
	return &catalogItemInterfaceDataSource{}
}

func (d *catalogItemInterfaceDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_catalog_item_interface"
}

func (d *catalogItemInterfaceDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "The inputs and outputs of the module version a Terrapod service-catalog item resolves to " +
			"(its version pin, or the latest uploaded version). Null while the module has no uploaded version.",
		Attributes: map[string]schema.Attribute{
			"catalog_item_id": schema.StringAttribute{
				Required:    true,
				Description: "The catalog item whose module interface to read.",
			},
			"resolved_version": schema.StringAttribute{
				Computed:    true,
				Description: "The module version the interface was read from.",
			},
			"interface_error": schema.StringAttribute{
				Computed: true,
				Description: "Why the module version's interface could not be read; null when it was. " +
					"When set, empty or partial inputs and outputs do not mean the module declares none.",
			},
			"inputs": schema.ListNestedAttribute{
				Computed:    true,
				Description: "The module's input variables.",
				NestedObject: schema.NestedAttributeObject{
					Attributes: map[string]schema.Attribute{
						"name":        schema.StringAttribute{Computed: true, Description: "Variable name."},
						"type":        schema.StringAttribute{Computed: true, Description: "Declared type, as written in the module."},
						"description": schema.StringAttribute{Computed: true, Description: "Variable description."},
						// Marked statically: the data source faithfully records, per
						// input, whether the module declared it sensitive — and then
						// stored that input's default in a non-sensitive attribute
						// immediately beside it. Per-element marking is unavailable
						// in a ListNestedAttribute, so the flag applies to every
						// element's default (GHSA-9646-883f-wjjm).
						"default":   schema.StringAttribute{Computed: true, Sensitive: true, Description: "Default value, JSON-encoded; null when the variable has none."},
						"required":  schema.BoolAttribute{Computed: true, Description: "Whether a value must be supplied."},
						"sensitive": schema.BoolAttribute{Computed: true, Description: "Whether the variable is marked sensitive."},
					},
				},
			},
			"outputs": schema.ListNestedAttribute{
				Computed:    true,
				Description: "The module's outputs.",
				NestedObject: schema.NestedAttributeObject{
					Attributes: map[string]schema.Attribute{
						"name":        schema.StringAttribute{Computed: true, Description: "Output name."},
						"description": schema.StringAttribute{Computed: true, Description: "Output description."},
						"sensitive":   schema.BoolAttribute{Computed: true, Description: "Whether the output is marked sensitive."},
					},
				},
			},
		},
	}
}

func (d *catalogItemInterfaceDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
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

func (d *catalogItemInterfaceDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config catalogItemInterfaceModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	iface, err := d.tc.GetCatalogItemInterface(ctx, config.CatalogItemID.ValueString())
	if err != nil {
		resp.Diagnostics.AddError("Failed to read catalog item interface", err.Error())
		return
	}

	config.ResolvedVersion = types.StringNull()
	if iface.ResolvedVersion != "" {
		config.ResolvedVersion = types.StringValue(iface.ResolvedVersion)
	}
	config.InterfaceError = interfaceError(iface.InterfaceError)

	config.Inputs = nil
	if iface.Inputs != nil {
		config.Inputs = []inputEntry{}
		for _, in := range iface.Inputs {
			config.Inputs = append(config.Inputs, inputEntry{
				Name:        types.StringValue(str(in, "name")),
				Type:        types.StringValue(str(in, "type")),
				Description: types.StringValue(str(in, "description")),
				Default:     defaultValue(in["default"]),
				Required:    types.BoolValue(flag(in, "required")),
				Sensitive:   types.BoolValue(flag(in, "sensitive")),
			})
		}
	}

	config.Outputs = nil
	if iface.Outputs != nil {
		config.Outputs = []outputEntry{}
		for _, out := range iface.Outputs {
			config.Outputs = append(config.Outputs, outputEntry{
				Name:        types.StringValue(str(out, "name")),
				Description: types.StringValue(str(out, "description")),
				Sensitive:   types.BoolValue(flag(out, "sensitive")),
			})
		}
	}

	resp.Diagnostics.Append(resp.State.Set(ctx, &config)...)
}

// interfaceError maps the API's null-when-fine reason (#1707) to a null
// attribute, so a clean interface never reads as an empty-string error.
func interfaceError(reason string) types.String {
	if reason == "" {
		return types.StringNull()
	}
	return types.StringValue(reason)
}

func str(m map[string]any, key string) string {
	if s, ok := m[key].(string); ok {
		return s
	}
	return ""
}

func flag(m map[string]any, key string) bool {
	b, _ := m[key].(bool)
	return b
}

// defaultValue renders an input's default as a string. The registry stores a
// default as a JSON-encoded string already; anything else is encoded here, and
// no default at all is null rather than "".
func defaultValue(v any) types.String {
	switch x := v.(type) {
	case nil:
		return types.StringNull()
	case string:
		return types.StringValue(x)
	default:
		b, err := json.Marshal(x)
		if err != nil {
			return types.StringNull()
		}
		return types.StringValue(string(b))
	}
}
