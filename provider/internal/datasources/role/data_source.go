// Package role implements the terrapod_role data source.
//
// API Contract: GET /api/v2/roles/{name}
// Looks up a single role by name.
package role

import (
	"context"
	"encoding/json"
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

	// The roles API uses "name" at the data level instead of "id",
	// so we can't use client.ParseResource which expects standard JSON:API.
	res, err := parseRoleResponse(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	config.Name = types.StringValue(res.Name)

	config.Description = stringOrNull(getStringFromMap(res.Attributes, "description"))
	config.WorkspacePermission = types.StringValue(getStringFromMap(res.Attributes, "workspace-permission"))
	config.BuiltIn = types.BoolValue(getBoolFromMap(res.Attributes, "built-in"))
	config.CreatedAt = types.StringValue(getStringFromMap(res.Attributes, "created-at"))
	config.UpdatedAt = types.StringValue(getStringFromMap(res.Attributes, "updated-at"))

	if raw, ok := res.Attributes["allow-labels"]; ok && raw != nil {
		if labels, ok := raw.(map[string]any); ok && len(labels) > 0 {
			strLabels := make(map[string]string, len(labels))
			for k, v := range labels {
				strLabels[k] = fmt.Sprintf("%v", v)
			}
			val, diags := types.MapValueFrom(ctx, types.StringType, strLabels)
			resp.Diagnostics.Append(diags...)
			config.AllowLabels = val
		} else {
			config.AllowLabels = types.MapNull(types.StringType)
		}
	} else {
		config.AllowLabels = types.MapNull(types.StringType)
	}

	if raw, ok := res.Attributes["deny-labels"]; ok && raw != nil {
		if labels, ok := raw.(map[string]any); ok && len(labels) > 0 {
			strLabels := make(map[string]string, len(labels))
			for k, v := range labels {
				strLabels[k] = fmt.Sprintf("%v", v)
			}
			val, diags := types.MapValueFrom(ctx, types.StringType, strLabels)
			resp.Diagnostics.Append(diags...)
			config.DenyLabels = val
		} else {
			config.DenyLabels = types.MapNull(types.StringType)
		}
	} else {
		config.DenyLabels = types.MapNull(types.StringType)
	}

	if raw, ok := res.Attributes["allow-names"]; ok && raw != nil {
		if names, ok := raw.([]any); ok && len(names) > 0 {
			strNames := make([]string, 0, len(names))
			for _, v := range names {
				strNames = append(strNames, fmt.Sprintf("%v", v))
			}
			val, diags := types.ListValueFrom(ctx, types.StringType, strNames)
			resp.Diagnostics.Append(diags...)
			config.AllowNames = val
		} else {
			config.AllowNames = types.ListNull(types.StringType)
		}
	} else {
		config.AllowNames = types.ListNull(types.StringType)
	}

	if raw, ok := res.Attributes["deny-names"]; ok && raw != nil {
		if names, ok := raw.([]any); ok && len(names) > 0 {
			strNames := make([]string, 0, len(names))
			for _, v := range names {
				strNames = append(strNames, fmt.Sprintf("%v", v))
			}
			val, diags := types.ListValueFrom(ctx, types.StringType, strNames)
			resp.Diagnostics.Append(diags...)
			config.DenyNames = val
		} else {
			config.DenyNames = types.ListNull(types.StringType)
		}
	} else {
		config.DenyNames = types.ListNull(types.StringType)
	}

	resp.Diagnostics.Append(resp.State.Set(ctx, &config)...)
}

// roleResponseData holds the parsed role response.
type roleResponseData struct {
	Name       string
	Attributes map[string]any
}

// parseRoleResponse extracts a role from a JSON:API response.
// The roles API uses "name" as the identifier, not "id".
func parseRoleResponse(data []byte) (*roleResponseData, error) {
	var doc struct {
		Data struct {
			Name       string         `json:"name"`
			Type       string         `json:"type"`
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, err
	}
	return &roleResponseData{
		Name:       doc.Data.Name,
		Attributes: doc.Data.Attributes,
	}, nil
}

func getStringFromMap(m map[string]any, key string) string {
	v, ok := m[key]
	if !ok || v == nil {
		return ""
	}
	s, ok := v.(string)
	if !ok {
		return fmt.Sprintf("%v", v)
	}
	return s
}

func getBoolFromMap(m map[string]any, key string) bool {
	v, ok := m[key]
	if !ok || v == nil {
		return false
	}
	b, ok := v.(bool)
	if !ok {
		return false
	}
	return b
}

func stringOrNull(s string) types.String {
	if s == "" {
		return types.StringNull()
	}
	return types.StringValue(s)
}
