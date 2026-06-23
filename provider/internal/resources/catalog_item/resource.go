// Package catalog_item implements the terrapod_catalog_item resource
// (service catalog, #535).
//
// API Contract (Terrapod API <-> Terraform Provider):
//
//	JSON:API type: "catalog-items"
//	ID format: bare UUID (no prefix)
//	Create:  POST   /api/terrapod/v1/catalog-items
//	Read:    GET    /api/terrapod/v1/catalog-items/{id}
//	Update:  PATCH  /api/terrapod/v1/catalog-items/{id}
//	Delete:  DELETE /api/terrapod/v1/catalog-items/{id}
//
// Attribute mapping (JSON:API attribute -> Terraform schema attribute):
//
//	"name"                   -> name                    (string, required)
//	"module-id"              -> module_id               (string, required)
//	"display-name"           -> display_name            (string, optional)
//	"description"            -> description             (string, optional)
//	"enabled"                -> enabled                 (bool, optional)
//	"default-version-pin"    -> default_version_pin     (string, optional)
//	"provider-template-ids"  -> provider_template_ids   (list(string), optional)
//	"allowed-agent-pool-ids" -> allowed_agent_pool_ids  (list(string), optional;
//	    omitted/null -> unrestricted)
//	"variable-options"       -> variable_options_json   (string, optional; JSON
//	    array of objects, kept as a JSON string to track the open-ended server
//	    contract)
//	"labels"                 -> labels                  (map[string]string, optional)
//
// Read-only:
//
//	"created-at"             -> created_at              (string, computed)
//	"updated-at"             -> updated_at              (string, computed)
//
// Import: by catalog item ID (bare UUID).
package catalog_item

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var (
	_ resource.Resource                = &catalogItemResource{}
	_ resource.ResourceWithImportState = &catalogItemResource{}
)

type catalogItemModel struct {
	ID types.String `tfsdk:"id"`

	Name                types.String `tfsdk:"name"`
	ModuleID            types.String `tfsdk:"module_id"`
	DisplayName         types.String `tfsdk:"display_name"`
	Description         types.String `tfsdk:"description"`
	Enabled             types.Bool   `tfsdk:"enabled"`
	DefaultVersionPin   types.String `tfsdk:"default_version_pin"`
	ProviderTemplateIDs types.List   `tfsdk:"provider_template_ids"`
	AllowedAgentPoolIDs types.List   `tfsdk:"allowed_agent_pool_ids"`
	VariableOptionsJSON types.String `tfsdk:"variable_options_json"`
	Labels              types.Map    `tfsdk:"labels"`

	CreatedAt types.String `tfsdk:"created_at"`
	UpdatedAt types.String `tfsdk:"updated_at"`
}

type catalogItemResource struct {
	client *client.Client
	tc     *terrapod.Client
}

// NewResource returns a new catalog item resource.
func NewResource() resource.Resource {
	return &catalogItemResource{}
}

func (r *catalogItemResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_catalog_item"
}

func (r *catalogItemResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a Terrapod service-catalog item — a blessed designation " +
			"over a registry module that users provision from without writing Terraform. " +
			"See https://github.com/mattrobinsonsre/terrapod/blob/main/docs/service-catalog.md.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description: "The catalog item ID (UUID).",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"name": schema.StringAttribute{
				Description: "Unique name for the catalog item.",
				Required:    true,
			},
			"module_id": schema.StringAttribute{
				Description: "The registry module ID this catalog item blesses.",
				Required:    true,
			},
			"display_name": schema.StringAttribute{
				Description: "Human-friendly display name shown in the catalog UI.",
				Optional:    true,
			},
			"description": schema.StringAttribute{
				Description: "Description of what provisioning this item produces.",
				Optional:    true,
			},
			"enabled": schema.BoolAttribute{
				Description: "Whether the catalog item is available for provisioning.",
				Optional:    true,
				Computed:    true,
			},
			"default_version_pin": schema.StringAttribute{
				Description: "Default module version pin offered when provisioning (e.g. \"~> 1.0\").",
				Optional:    true,
			},
			"provider_template_ids": schema.ListAttribute{
				Description: "Provider template IDs rendered into the generated wrapper for instances.",
				Optional:    true,
				ElementType: types.StringType,
			},
			"allowed_agent_pool_ids": schema.ListAttribute{
				Description: "Agent pool IDs users may provision instances onto. Omit (null) for unrestricted.",
				Optional:    true,
				ElementType: types.StringType,
			},
			"variable_options_json": schema.StringAttribute{
				Description: "Optional JSON array of variable-option objects controlling how the " +
					"provision form is rendered. Supplied as a JSON string to track the " +
					"open-ended server contract.",
				Optional: true,
			},
			"labels": schema.MapAttribute{
				Description: "Labels feeding Terrapod's label-based RBAC and filtering.",
				Optional:    true,
				ElementType: types.StringType,
			},

			// Read-only
			"created_at": schema.StringAttribute{
				Description: "Creation timestamp.",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"updated_at": schema.StringAttribute{
				Description: "Last update timestamp.",
				Computed:    true,
			},
		},
	}
}

func (r *catalogItemResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	c, ok := req.ProviderData.(*client.Client)
	if !ok {
		resp.Diagnostics.AddError("Unexpected provider data type", fmt.Sprintf("Expected *client.Client, got %T", req.ProviderData))
		return
	}
	r.client = c

	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: c.BaseURL, Token: c.Token})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	r.tc = tc
}

func (r *catalogItemResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan catalogItemModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs, diags := buildCatalogItemAttrs(&plan)
	resp.Diagnostics.Append(diags...)
	if resp.Diagnostics.HasError() {
		return
	}

	ci, err := r.tc.CreateCatalogItem(ctx, attrs)
	if err != nil {
		resp.Diagnostics.AddError("Failed to create catalog item", err.Error())
		return
	}

	resp.Diagnostics.Append(readCatalogItemIntoModel(ctx, ci, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *catalogItemResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state catalogItemModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	ci, err := r.tc.GetCatalogItem(ctx, state.ID.ValueString())
	if err != nil {
		if terrapod.IsNotFound(err) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Failed to read catalog item", err.Error())
		return
	}

	resp.Diagnostics.Append(readCatalogItemIntoModel(ctx, ci, &state)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *catalogItemResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan catalogItemModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var state catalogItemModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs, diags := buildCatalogItemAttrs(&plan)
	resp.Diagnostics.Append(diags...)
	if resp.Diagnostics.HasError() {
		return
	}

	ci, err := r.tc.UpdateCatalogItem(ctx, state.ID.ValueString(), attrs)
	if err != nil {
		resp.Diagnostics.AddError("Failed to update catalog item", err.Error())
		return
	}

	resp.Diagnostics.Append(readCatalogItemIntoModel(ctx, ci, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *catalogItemResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state catalogItemModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	err := r.tc.DeleteCatalogItem(ctx, state.ID.ValueString())
	if err != nil && !terrapod.IsNotFound(err) {
		resp.Diagnostics.AddError("Failed to delete catalog item", err.Error())
	}
}

func (r *catalogItemResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

// buildCatalogItemAttrs converts the Terraform model into JSON:API
// attributes for create/update. Computed-only attributes are omitted.
func buildCatalogItemAttrs(m *catalogItemModel) (map[string]any, diag.Diagnostics) {
	var diags diag.Diagnostics

	attrs := map[string]any{
		"name":      m.Name.ValueString(),
		"module-id": m.ModuleID.ValueString(),
	}

	if !m.DisplayName.IsNull() && !m.DisplayName.IsUnknown() {
		attrs["display-name"] = m.DisplayName.ValueString()
	}
	if !m.Description.IsNull() && !m.Description.IsUnknown() {
		attrs["description"] = m.Description.ValueString()
	}
	if !m.Enabled.IsNull() && !m.Enabled.IsUnknown() {
		attrs["enabled"] = m.Enabled.ValueBool()
	}
	if !m.DefaultVersionPin.IsNull() && !m.DefaultVersionPin.IsUnknown() {
		attrs["default-version-pin"] = m.DefaultVersionPin.ValueString()
	}

	if !m.ProviderTemplateIDs.IsNull() && !m.ProviderTemplateIDs.IsUnknown() {
		attrs["provider-template-ids"] = listToStrings(m.ProviderTemplateIDs)
	}

	// Omit the allow-list entirely when null/unset so the server treats the
	// item as unrestricted.
	if !m.AllowedAgentPoolIDs.IsNull() && !m.AllowedAgentPoolIDs.IsUnknown() {
		attrs["allowed-agent-pool-ids"] = listToStrings(m.AllowedAgentPoolIDs)
	}

	if !m.VariableOptionsJSON.IsNull() && !m.VariableOptionsJSON.IsUnknown() && m.VariableOptionsJSON.ValueString() != "" {
		var opts []any
		if err := json.Unmarshal([]byte(m.VariableOptionsJSON.ValueString()), &opts); err != nil {
			diags.AddError("Invalid variable_options_json", fmt.Sprintf("variable_options_json must be a JSON array of objects: %s", err))
			return nil, diags
		}
		attrs["variable-options"] = opts
	}

	if !m.Labels.IsNull() && !m.Labels.IsUnknown() {
		labels := map[string]string{}
		for k, v := range m.Labels.Elements() {
			labels[k] = v.(types.String).ValueString()
		}
		attrs["labels"] = labels
	}

	return attrs, diags
}

// listToStrings flattens a types.List of strings into a []string.
func listToStrings(l types.List) []string {
	out := make([]string, 0, len(l.Elements()))
	for _, v := range l.Elements() {
		out = append(out, v.(types.String).ValueString())
	}
	return out
}

// readCatalogItemIntoModel maps the SDK shape into the Terraform model.
// Open-ended attributes are read out of the Attributes map.
func readCatalogItemIntoModel(ctx context.Context, ci *terrapod.CatalogItem, m *catalogItemModel) diag.Diagnostics {
	var diags diag.Diagnostics

	m.ID = types.StringValue(ci.ID)
	m.Name = types.StringValue(attrString(ci.Attributes, "name"))
	m.ModuleID = types.StringValue(attrString(ci.Attributes, "module-id"))

	m.DisplayName = optString(ci.Attributes, "display-name")
	m.Description = optString(ci.Attributes, "description")

	if v, ok := ci.Attributes["enabled"]; ok && v != nil {
		if b, ok := v.(bool); ok {
			m.Enabled = types.BoolValue(b)
		} else {
			m.Enabled = types.BoolNull()
		}
	} else {
		m.Enabled = types.BoolNull()
	}

	m.DefaultVersionPin = optString(ci.Attributes, "default-version-pin")

	ptIDs := attrStringList(ci.Attributes, "provider-template-ids")
	if len(ptIDs) > 0 {
		lv, d := types.ListValueFrom(ctx, types.StringType, ptIDs)
		diags.Append(d...)
		m.ProviderTemplateIDs = lv
	} else {
		m.ProviderTemplateIDs = types.ListNull(types.StringType)
	}

	poolIDs := attrStringList(ci.Attributes, "allowed-agent-pool-ids")
	if len(poolIDs) > 0 {
		lv, d := types.ListValueFrom(ctx, types.StringType, poolIDs)
		diags.Append(d...)
		m.AllowedAgentPoolIDs = lv
	} else {
		m.AllowedAgentPoolIDs = types.ListNull(types.StringType)
	}

	if raw, ok := ci.Attributes["variable-options"]; ok && raw != nil {
		if arr, ok := raw.([]any); ok && len(arr) > 0 {
			b, err := json.Marshal(arr)
			if err != nil {
				diags.AddError("Failed to encode variable-options", err.Error())
				return diags
			}
			m.VariableOptionsJSON = types.StringValue(string(b))
		} else {
			m.VariableOptionsJSON = types.StringNull()
		}
	} else {
		m.VariableOptionsJSON = types.StringNull()
	}

	labels := attrStringMap(ci.Attributes, "labels")
	if len(labels) > 0 {
		mv, dl := types.MapValueFrom(ctx, types.StringType, labels)
		diags.Append(dl...)
		m.Labels = mv
	} else {
		m.Labels = types.MapNull(types.StringType)
	}

	m.CreatedAt = types.StringValue(ci.CreatedAt)
	m.UpdatedAt = types.StringValue(ci.UpdatedAt)

	return diags
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

// optString reads a string attribute into a types.String, returning null
// when absent/empty so a config that omits the field matches state.
func optString(attrs map[string]any, key string) types.String {
	if s := attrString(attrs, key); s != "" {
		return types.StringValue(s)
	}
	return types.StringNull()
}

// attrStringList reads a list-of-strings attribute out of an SDK
// Attributes map. Returns nil when absent/empty.
func attrStringList(attrs map[string]any, key string) []string {
	v, ok := attrs[key]
	if !ok || v == nil {
		return nil
	}
	arr, ok := v.([]any)
	if !ok || len(arr) == 0 {
		return nil
	}
	out := make([]string, 0, len(arr))
	for _, e := range arr {
		if s, ok := e.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

// attrStringMap reads a map[string]string attribute (e.g. labels) out of
// an SDK Attributes map, coercing values to strings. Returns nil when
// absent or empty.
func attrStringMap(attrs map[string]any, key string) map[string]string {
	v, ok := attrs[key]
	if !ok || v == nil {
		return nil
	}
	raw, ok := v.(map[string]any)
	if !ok || len(raw) == 0 {
		return nil
	}
	out := make(map[string]string, len(raw))
	for k, val := range raw {
		if s, ok := val.(string); ok {
			out[k] = s
		}
	}
	return out
}
