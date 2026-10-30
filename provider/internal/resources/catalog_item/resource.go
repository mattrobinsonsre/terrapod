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
//	"configure-enabled"      -> configure_enabled       (bool, optional+computed)
//	"ansible-directory"      -> ansible_directory       (string, optional+computed)
//	"default-playbook"       -> default_playbook        (string, optional+computed)
//	"default-limit"          -> default_limit           (string, optional+computed)
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
	_ resource.Resource                   = &catalogItemResource{}
	_ resource.ResourceWithImportState    = &catalogItemResource{}
	_ resource.ResourceWithValidateConfig = &catalogItemResource{}
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

	// The Ansible half this item provisions (#2019): a workspace that is
	// primarily Ansible, carrying only as much Terraform as it takes to declare
	// the inventory. ConfigureEnabled says WHETHER and AnsibleDirectory says
	// WHERE; an empty directory does not carry "off", because #1966 corrected
	// exactly that on the workspace's own config_directory.
	ConfigureEnabled types.Bool   `tfsdk:"configure_enabled"`
	AnsibleDirectory types.String `tfsdk:"ansible_directory"`
	DefaultPlaybook  types.String `tfsdk:"default_playbook"`
	DefaultLimit     types.String `tfsdk:"default_limit"`

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

// ValidateConfig rejects a config that asks for a configure operation without
// saying where the playbook lives.
//
// The server refuses the same pair with a 422 (#2019); catching it at plan time
// turns an apply-time failure into an error the operator sees before anything is
// sent, which is the precedent the workspace resource sets with
// validateOperationsPair.
func (r *catalogItemResource) ValidateConfig(ctx context.Context, req resource.ValidateConfigRequest, resp *resource.ValidateConfigResponse) {
	var m catalogItemModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	validateConfigureDirectoryPair(m.ConfigureEnabled, m.AnsibleDirectory, resp)
}

// validateConfigureDirectoryPair errors when a config turns the configure
// operation on and declares an empty directory in the same breath (#2019).
//
// An OMITTED directory is deliberately not an error here. The server evaluates
// the invariant against the row the write would PRODUCE, which is the authority:
// only it knows which directory the item already holds, so switching
// `configure_enabled` on is legitimate against a stored one. A null is not a
// declaration of emptiness -- it is "leave whatever is there" on update and
// "take the default" on create -- and ValidateConfig sees the configuration
// alone, with no state to tell the two apart. So this catches the subset that is
// decidable from the configuration, and the rest stays the server's 422, exactly
// as validateOperationsPair leaves the workspace's unknown half to the server.
func validateConfigureDirectoryPair(enabled types.Bool, directory types.String, resp *resource.ValidateConfigResponse) {
	if enabled.IsNull() || enabled.IsUnknown() || !enabled.ValueBool() {
		return
	}
	if directory.IsNull() || directory.IsUnknown() {
		return
	}
	if directory.ValueString() == "" {
		resp.Diagnostics.AddError(
			"configure_enabled needs an ansible_directory",
			"This configuration sets `configure_enabled = true` with an empty "+
				"`ansible_directory`, which would provision a workspace whose Ansible root "+
				"is the configuration version's root -- where only the generated wrapper "+
				"lives, so a configure would fail at run time for want of a playbook. Name "+
				"the subdirectory of the module that holds the playbook.",
		)
	}
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
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"description": schema.StringAttribute{
				Description: "Description of what provisioning this item produces.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"enabled": schema.BoolAttribute{
				Description: "Whether the catalog item is available for provisioning.",
				Optional:    true,
				Computed:    true,
			},
			"default_version_pin": schema.StringAttribute{
				Description: "Default module version pin offered when provisioning (e.g. \"~> 1.0\").",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
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
				// Same shape as provider_template.parameters_json: an opaque JSON
				// string whose objects may carry a sensitive default, and a schema
				// cannot mark sub-fields of one.
				Sensitive: true,
			},

			// The Ansible half this item provisions (#2019). `configure_enabled`
			// says whether and `ansible_directory` says where.
			//
			// Requiredness is uniform across this resource's settable strings:
			// Optional+Computed with UseStateForUnknown, read back concretely. The
			// bool is Optional+Computed like `enabled` -- the server always returns
			// one, so a null plan against a `false` result would fail the apply --
			// and #2307 brought `display_name`, `description` and
			// `default_version_pin` onto the same shape, so there is no longer a
			// second convention in this file to choose between.
			"configure_enabled": schema.BoolAttribute{
				Description: "Whether a workspace provisioned from this item has the Ansible " +
					"configure operation. Defaults to false — configure is opt-in. " +
					"**Turning it on needs a non-empty `ansible_directory`; the pair is " +
					"refused (422) otherwise**, because a configure rooted at the generated " +
					"wrapper has no playbook to run.",
				Optional: true,
				Computed: true,
			},
			"ansible_directory": schema.StringAttribute{
				Description: "Subdirectory of the MODULE whose contents are copied into the " +
					"generated configuration version, and the `config_directory` the " +
					"provisioned workspace is given. Module-relative, written without a " +
					"leading or trailing slash. Copied rather than referenced because a " +
					"configure Job never runs init, so the module's own checkout does not " +
					"exist in it.",
				Optional: true,
				Computed: true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"default_playbook": schema.StringAttribute{
				Description: "Playbook templated onto the provisioned workspace, relative to " +
					"`ansible_directory` — which becomes that workspace's Ansible root. " +
					"Written without a leading or trailing slash.",
				Optional: true,
				Computed: true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"default_limit": schema.StringAttribute{
				Description: "Ansible `--limit` templated onto the provisioned workspace. " +
					"Stored byte for byte, so leading or trailing whitespace is refused " +
					"rather than trimmed.",
				Optional: true,
				Computed: true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
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

	// The Ansible half (#2019). Each is sent only when the config declares it:
	// the server validates what it is sent and leaves the rest of the row alone,
	// so a PATCH of something else cannot be refused over a stored value it never
	// mentioned (#316).
	//
	// Sent VERBATIM. The server stores these byte for byte and this is the other
	// half of that bargain: trimming or lower-casing a value on the way out is how
	// the state written back stops matching the plan, which fails the apply with
	// "Provider produced inconsistent result after apply". A value the server would
	// refuse is left to the server to refuse.
	if !m.ConfigureEnabled.IsNull() && !m.ConfigureEnabled.IsUnknown() {
		attrs["configure-enabled"] = m.ConfigureEnabled.ValueBool()
	}
	if !m.AnsibleDirectory.IsNull() && !m.AnsibleDirectory.IsUnknown() {
		attrs["ansible-directory"] = m.AnsibleDirectory.ValueString()
	}
	if !m.DefaultPlaybook.IsNull() && !m.DefaultPlaybook.IsUnknown() {
		attrs["default-playbook"] = m.DefaultPlaybook.ValueString()
	}
	if !m.DefaultLimit.IsNull() && !m.DefaultLimit.IsUnknown() {
		attrs["default-limit"] = m.DefaultLimit.ValueString()
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

	m.DisplayName = types.StringValue(attrString(ci.Attributes, "display-name"))
	m.Description = types.StringValue(attrString(ci.Attributes, "description"))

	if v, ok := ci.Attributes["enabled"]; ok && v != nil {
		if b, ok := v.(bool); ok {
			m.Enabled = types.BoolValue(b)
		} else {
			m.Enabled = types.BoolNull()
		}
	} else {
		m.Enabled = types.BoolNull()
	}

	m.DefaultVersionPin = types.StringValue(attrString(ci.Attributes, "default-version-pin"))

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

	// The Ansible half (#2019), read back unchanged. The bool follows `enabled`
	// above; the strings follow the concrete read-back below.
	if v, ok := ci.Attributes["configure-enabled"]; ok && v != nil {
		if b, ok := v.(bool); ok {
			m.ConfigureEnabled = types.BoolValue(b)
		} else {
			m.ConfigureEnabled = types.BoolNull()
		}
	} else {
		m.ConfigureEnabled = types.BoolNull()
	}
	// CONCRETE for every settable string on this resource (#2307). They are
	// Optional+Computed, so an omitted one plans as unknown and any concrete
	// result satisfies it -- while an EXPLICIT "" must read back as "" or the
	// apply fails "Provider produced inconsistent result after apply".
	//
	// This file used to carry an `optString` that collapsed "" to null, which
	// shipped exactly that defect on `display_name`, `description` and
	// `default_version_pin`. It had no other callers, so #2307 deleted the
	// helper rather than only its uses: the mechanism is gone, not patched.
	//
	// Not local tidiness: `default_playbook` and `default_limit` are the SAME
	// attribute names on `terrapod_workspace`, where they are Optional+Computed
	// with a concrete read-back. One name behaving two ways across two resources
	// is worse than one convention here, and the Optional-only shape ships the
	// "inconsistent result" defect this provider has already closed six times
	// (#684, #732, #1634, #1748, #1771, #2110).
	m.AnsibleDirectory = types.StringValue(attrString(ci.Attributes, "ansible-directory"))
	m.DefaultPlaybook = types.StringValue(attrString(ci.Attributes, "default-playbook"))
	m.DefaultLimit = types.StringValue(attrString(ci.Attributes, "default-limit"))

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
