// Package provider_template implements the terrapod_provider_template
// resource (service catalog, #535).
//
// API Contract (Terrapod API <-> Terraform Provider):
//
//	JSON:API type: "provider-templates"
//	ID format: bare UUID (no prefix)
//	Create:  POST   /api/v1/provider-templates
//	Read:    GET    /api/v1/provider-templates/{id}
//	Update:  PATCH  /api/v1/provider-templates/{id}
//	Delete:  DELETE /api/v1/provider-templates/{id}
//
// Attribute mapping (JSON:API attribute -> Terraform schema attribute):
//
//	"name"          -> name             (string, required)
//	"provider-type" -> provider_type    (string, required)
//	"body"          -> body             (string, required, HCL)
//	"parameters"    -> parameters_json  (string, optional; JSON array of
//	    objects like {name,type,description,required,sensitive,default,
//	    options}). Modeled as a JSON string to keep the schema simple and
//	    track the open-ended server contract.
//	"labels"        -> labels           (map[string]string, optional)
//
// Read-only:
//
//	"created-at"    -> created_at       (string, computed)
//	"updated-at"    -> updated_at       (string, computed)
//
// Import: by template ID (bare UUID).
package provider_template

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"

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
	_ resource.Resource                = &providerTemplateResource{}
	_ resource.ResourceWithImportState = &providerTemplateResource{}
)

type providerTemplateModel struct {
	ID types.String `tfsdk:"id"`

	Name         types.String     `tfsdk:"name"`
	ProviderType types.String     `tfsdk:"provider_type"`
	Body         types.String     `tfsdk:"body"`
	Parameters   []parameterModel `tfsdk:"parameter"`
	Labels       types.Map        `tfsdk:"labels"`

	CreatedAt types.String `tfsdk:"created_at"`
	UpdatedAt types.String `tfsdk:"updated_at"`
}

// parameterModel is one `parameter` block. Every field is Optional but `name`,
// matching the server, which fills the rest in.
type parameterModel struct {
	Name        types.String `tfsdk:"name"`
	Type        types.String `tfsdk:"type"`
	Description types.String `tfsdk:"description"`
	Required    types.Bool   `tfsdk:"required"`
	Sensitive   types.Bool   `tfsdk:"sensitive"`
	DefaultJSON types.String `tfsdk:"default_json"`
	OptionsJSON types.String `tfsdk:"options_json"`
}

type providerTemplateResource struct {
	client *client.Client
	tc     *terrapod.Client
}

// NewResource returns a new provider template resource.
func NewResource() resource.Resource {
	return &providerTemplateResource{}
}

func (r *providerTemplateResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_provider_template"
}

func (r *providerTemplateResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a Terrapod service-catalog provider template — an " +
			"admin-managed, parameterised provider config rendered into a catalog " +
			"instance's generated wrapper (providers.tf). See " +
			"https://github.com/mattrobinsonsre/terrapod/blob/main/docs/service-catalog.md.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description: "The provider template ID (UUID).",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"name": schema.StringAttribute{
				Description: "Display name for the provider template.",
				Required:    true,
			},
			"provider_type": schema.StringAttribute{
				Description: "The provider type this template configures (e.g. \"aws\", \"google\").",
				Required:    true,
			},
			"body": schema.StringAttribute{
				Description: "HCL body rendered into the generated provider block.",
				Required:    true,
				// The canonical place for cloud credentials — this renders a
				// `provider {}` block, so `secret_key` / `token` live here — and
				// the server's copy is read back into state on every refresh
				// (GHSA-9646-883f-wjjm).
				Sensitive: true,
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
		Blocks: map[string]schema.Block{
			// A BLOCK, not a JSON string (#2681). The string forced one blunt
			// `Sensitive` over the whole parameter list -- so a plan redacted every
			// name, type and description even when no parameter was secret -- left a
			// misspelled field validated by nobody at either end, and re-diffed the
			// whole blob when one description changed.
			//
			// Blocks rather than nested attributes is the maintainer's call for this
			// provider: blocks and `for_each` are idiomatic HCL, and a major release
			// is when the shape can change.
			"parameter": schema.ListNestedBlock{
				Description: "A parameter the template body interpolates. Ordered, because " +
					"that is the order of the fields on the provision form.",
				NestedObject: schema.NestedBlockObject{
					Attributes: map[string]schema.Attribute{
						"name": schema.StringAttribute{
							Description: "The parameter name, as the template body interpolates it.",
							Required:    true,
						},
						"type": schema.StringAttribute{
							Description: "The value's type, for form rendering. Defaults to string.",
							Optional:    true,
						},
						"description": schema.StringAttribute{
							Description: "What to tell the provisioner on the form.",
							Optional:    true,
						},
						"required": schema.BoolAttribute{
							Description: "Whether a provision must supply it. Defaults to false.",
							Optional:    true,
						},
						"sensitive": schema.BoolAttribute{
							Description: "Mask the value on the form and in responses. Defaults to false.",
							Optional:    true,
						},
						"default_json": schema.StringAttribute{
							Description: "The value to pre-fill when a provision does not supply " +
								"one, as a JSON document: `jsonencode(5)`, `jsonencode(\"eu-west-1\")`. " +
								"JSON because the server accepts any type here, and " +
								"terraform-plugin-framework does not permit a dynamic type inside a " +
								"repeated block.",
							Optional: true,
							// Narrower than the whole-attribute flag it replaces: only the VALUE
							// is redacted, so names, types and descriptions stay readable in a
							// plan -- which is what an operator reviews, and which the old opaque
							// string hid along with the secret. Still blunt, because Terraform
							// cannot mark one attribute sensitive on a SIBLING's value, so every
							// default is masked rather than only a secret parameter's.
							Sensitive: true,
						},
						"options_json": schema.StringAttribute{
							Description: "Restrict the parameter to a closed list of permitted " +
								"values, as a JSON array: `jsonencode([\"a\", \"b\"])`. Omit for a " +
								"free-text field.",
							Optional: true,
						},
					},
				},
			},
		},
	}
}

func (r *providerTemplateResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *providerTemplateResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan providerTemplateModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs, diags := buildProviderTemplateAttrs(ctx, &plan)
	resp.Diagnostics.Append(diags...)
	if resp.Diagnostics.HasError() {
		return
	}

	pt, err := r.tc.CreateProviderTemplate(ctx, attrs)
	if err != nil {
		resp.Diagnostics.AddError("Failed to create provider template", err.Error())
		return
	}
	pt, err = r.readAuthoring(ctx, pt.ID)
	if err != nil {
		resp.Diagnostics.AddError("Failed to read created provider template", err.Error())
		return
	}

	resp.Diagnostics.Append(readProviderTemplateIntoModel(ctx, pt, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *providerTemplateResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state providerTemplateModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	pt, err := r.readAuthoring(ctx, state.ID.ValueString())
	if err != nil {
		if terrapod.IsNotFound(err) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Failed to read provider template", err.Error())
		return
	}

	resp.Diagnostics.Append(readProviderTemplateIntoModel(ctx, pt, &state)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *providerTemplateResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan providerTemplateModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var state providerTemplateModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs, diags := buildProviderTemplateAttrs(ctx, &plan)
	resp.Diagnostics.Append(diags...)
	if resp.Diagnostics.HasError() {
		return
	}

	pt, err := r.tc.UpdateProviderTemplate(ctx, state.ID.ValueString(), attrs)
	if err != nil {
		resp.Diagnostics.AddError("Failed to update provider template", err.Error())
		return
	}
	pt, err = r.readAuthoring(ctx, pt.ID)
	if err != nil {
		resp.Diagnostics.AddError("Failed to read updated provider template", err.Error())
		return
	}

	resp.Diagnostics.Append(readProviderTemplateIntoModel(ctx, pt, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *providerTemplateResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state providerTemplateModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	err := r.tc.DeleteProviderTemplate(ctx, state.ID.ValueString())
	if err != nil && !terrapod.IsNotFound(err) {
		resp.Diagnostics.AddError("Failed to delete provider template", err.Error())
	}
}

func (r *providerTemplateResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

func (r *providerTemplateResource) readAuthoring(ctx context.Context, id string) (*terrapod.ProviderTemplate, error) {
	// The ordinary SDK read masks the body and defaults; state needs both.
	data, err := r.tc.Get(ctx, "/api/v1/provider-templates/"+url.PathEscape(id)+"?include-sensitive-values=true")
	if err != nil {
		return nil, err
	}
	res, err := terrapod.ParseResource(data)
	if err != nil {
		return nil, err
	}
	attrs := map[string]any{}
	for key, raw := range res.Attributes {
		var value any
		if err := json.Unmarshal(raw, &value); err != nil {
			return nil, fmt.Errorf("parse provider template attribute %q: %w", key, err)
		}
		attrs[key] = value
	}
	return &terrapod.ProviderTemplate{ID: res.ID, Attributes: attrs, CreatedAt: attrString(attrs, "created-at"), UpdatedAt: attrString(attrs, "updated-at")}, nil
}

// buildProviderTemplateAttrs converts the Terraform model into JSON:API
// attributes for create/update. Computed-only attributes are omitted.
func buildProviderTemplateAttrs(ctx context.Context, m *providerTemplateModel) (map[string]any, diag.Diagnostics) {
	var diags diag.Diagnostics

	attrs := map[string]any{
		"name":          m.Name.ValueString(),
		"provider-type": m.ProviderType.ValueString(),
		"body":          m.Body.ValueString(),
	}

	if len(m.Parameters) > 0 {
		params := make([]any, 0, len(m.Parameters))
		for i, p := range m.Parameters {
			one := map[string]any{"name": p.Name.ValueString()}
			// Each optional scalar is sent only when written, so the server's own
			// default fills the rest. Sending a zero value instead would make
			// "I did not say" indistinguishable from "false".
			if !p.Type.IsNull() && !p.Type.IsUnknown() {
				one["type"] = p.Type.ValueString()
			}
			if !p.Description.IsNull() && !p.Description.IsUnknown() {
				one["description"] = p.Description.ValueString()
			}
			if !p.Required.IsNull() && !p.Required.IsUnknown() {
				one["required"] = p.Required.ValueBool()
			}
			if !p.Sensitive.IsNull() && !p.Sensitive.IsUnknown() {
				one["sensitive"] = p.Sensitive.ValueBool()
			}
			if v, ok := decodeJSON(&diags, p.DefaultJSON, "parameter", p.Name.ValueString(), "default_json"); !ok {
				return nil, diags
			} else if v != nil {
				one["default"] = *v
			}
			if v, ok := decodeJSON(&diags, p.OptionsJSON, "parameter", p.Name.ValueString(), "options_json"); !ok {
				return nil, diags
			} else if v != nil {
				if _, isList := (*v).([]any); !isList {
					diags.AddError(
						"Invalid parameter options_json",
						fmt.Sprintf("parameter %q: options_json must be a JSON array, got %T",
							p.Name.ValueString(), *v),
					)
					return nil, diags
				}
				one["options"] = *v
			}
			_ = i
			params = append(params, one)
		}
		attrs["parameters"] = params
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

// readProviderTemplateIntoModel maps the SDK shape into the Terraform
// model. Open-ended attributes are read out of the Attributes map.
func readProviderTemplateIntoModel(ctx context.Context, pt *terrapod.ProviderTemplate, m *providerTemplateModel) diag.Diagnostics {
	var diags diag.Diagnostics

	m.ID = types.StringValue(pt.ID)
	m.Name = types.StringValue(attrString(pt.Attributes, "name"))
	m.ProviderType = types.StringValue(attrString(pt.Attributes, "provider-type"))
	m.Body = types.StringValue(attrString(pt.Attributes, "body"))

	// Keep the model's block list EMPTY when the server returns no parameters, so
	// a configuration that writes no `parameter` block matches what comes back.
	m.Parameters = nil
	if raw, ok := pt.Attributes["parameters"]; ok && raw != nil {
		arr, _ := raw.([]any)
		for i, entry := range arr {
			obj, ok := entry.(map[string]any)
			if !ok {
				diags.AddError(
					"Unexpected parameter",
					fmt.Sprintf("parameter %d is %T rather than an object", i, entry),
				)
				return diags
			}
			one := parameterModel{
				Name:        optString(obj, "name"),
				Type:        optString(obj, "type"),
				Description: optString(obj, "description"),
				Required:    optBool(obj, "required"),
				Sensitive:   optBool(obj, "sensitive"),
			}
			one.DefaultJSON = jsonOrNull(obj["default"])
			one.OptionsJSON = jsonOrNull(obj["options"])
			m.Parameters = append(m.Parameters, one)
		}
	}

	labels := attrStringMap(pt.Attributes, "labels")
	if len(labels) > 0 {
		mv, dl := types.MapValueFrom(ctx, types.StringType, labels)
		diags.Append(dl...)
		m.Labels = mv
	} else {
		m.Labels = types.MapNull(types.StringType)
	}

	m.CreatedAt = types.StringValue(pt.CreatedAt)
	m.UpdatedAt = types.StringValue(pt.UpdatedAt)

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

// optString reads an optional string, leaving it NULL when the server omitted
// it rather than defaulting to "" -- an empty description a practitioner never
// wrote would otherwise show as a diff on every plan.
func optString(obj map[string]any, key string) types.String {
	if v, ok := obj[key]; ok && v != nil {
		if s, ok := v.(string); ok && s != "" {
			return types.StringValue(s)
		}
	}
	return types.StringNull()
}

// optBool is the same for a flag: absent stays null, so a server that omits
// `required` does not read as an explicit false.
func optBool(obj map[string]any, key string) types.Bool {
	if v, ok := obj[key]; ok && v != nil {
		if b, ok := v.(bool); ok {
			return types.BoolValue(b)
		}
	}
	return types.BoolNull()
}

// decodeJSON reads one of the two JSON-encoded leaves. It returns (nil, true)
// when the attribute is unset, so an absent key is omitted from the request --
// the server reads a missing key as "not supplied" and a present null as "set it
// to null", and those are different requests.
func decodeJSON(diags *diag.Diagnostics, raw types.String, kind, name, attr string) (*any, bool) {
	if raw.IsNull() || raw.IsUnknown() || raw.ValueString() == "" {
		return nil, true
	}
	var v any
	if err := json.Unmarshal([]byte(raw.ValueString()), &v); err != nil {
		diags.AddError(
			fmt.Sprintf("Invalid %s %s", kind, attr),
			fmt.Sprintf("%s %q: %s must be a JSON document: %s", kind, name, attr, err),
		)
		return nil, false
	}
	return &v, true
}

// jsonOrNull re-encodes what the server returned, leaving the attribute NULL
// when there was none -- so a parameter with no default does not read back as
// the four-character string "null" and diff against a config that omits it.
func jsonOrNull(v any) types.String {
	if v == nil {
		return types.StringNull()
	}
	b, err := json.Marshal(v)
	if err != nil {
		return types.StringNull()
	}
	return types.StringValue(string(b))
}
