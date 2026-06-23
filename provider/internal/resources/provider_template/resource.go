// Package provider_template implements the terrapod_provider_template
// resource (service catalog, #535).
//
// API Contract (Terrapod API <-> Terraform Provider):
//
//	JSON:API type: "provider-templates"
//	ID format: bare UUID (no prefix)
//	Create:  POST   /api/terrapod/v1/provider-templates
//	Read:    GET    /api/terrapod/v1/provider-templates/{id}
//	Update:  PATCH  /api/terrapod/v1/provider-templates/{id}
//	Delete:  DELETE /api/terrapod/v1/provider-templates/{id}
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

	Name           types.String `tfsdk:"name"`
	ProviderType   types.String `tfsdk:"provider_type"`
	Body           types.String `tfsdk:"body"`
	ParametersJSON types.String `tfsdk:"parameters_json"`
	Labels         types.Map    `tfsdk:"labels"`

	CreatedAt types.String `tfsdk:"created_at"`
	UpdatedAt types.String `tfsdk:"updated_at"`
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
			},
			"parameters_json": schema.StringAttribute{
				Description: "Optional JSON array of parameter objects (each like " +
					"{name,type,description,required,sensitive,default,options}). " +
					"Supplied as a JSON string to track the open-ended server contract.",
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

	attrs, diags := buildProviderTemplateAttrs(&plan)
	resp.Diagnostics.Append(diags...)
	if resp.Diagnostics.HasError() {
		return
	}

	pt, err := r.tc.CreateProviderTemplate(ctx, attrs)
	if err != nil {
		resp.Diagnostics.AddError("Failed to create provider template", err.Error())
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

	pt, err := r.tc.GetProviderTemplate(ctx, state.ID.ValueString())
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

	attrs, diags := buildProviderTemplateAttrs(&plan)
	resp.Diagnostics.Append(diags...)
	if resp.Diagnostics.HasError() {
		return
	}

	pt, err := r.tc.UpdateProviderTemplate(ctx, state.ID.ValueString(), attrs)
	if err != nil {
		resp.Diagnostics.AddError("Failed to update provider template", err.Error())
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

// buildProviderTemplateAttrs converts the Terraform model into JSON:API
// attributes for create/update. Computed-only attributes are omitted.
func buildProviderTemplateAttrs(m *providerTemplateModel) (map[string]any, diag.Diagnostics) {
	var diags diag.Diagnostics

	attrs := map[string]any{
		"name":          m.Name.ValueString(),
		"provider-type": m.ProviderType.ValueString(),
		"body":          m.Body.ValueString(),
	}

	if !m.ParametersJSON.IsNull() && !m.ParametersJSON.IsUnknown() && m.ParametersJSON.ValueString() != "" {
		var params []any
		if err := json.Unmarshal([]byte(m.ParametersJSON.ValueString()), &params); err != nil {
			diags.AddError("Invalid parameters_json", fmt.Sprintf("parameters_json must be a JSON array of objects: %s", err))
			return nil, diags
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

	// parameters round-trips as a JSON string. Keep the model null when the
	// server returns no parameters so a config that omits the field matches.
	if raw, ok := pt.Attributes["parameters"]; ok && raw != nil {
		if arr, ok := raw.([]any); ok && len(arr) > 0 {
			b, err := json.Marshal(arr)
			if err != nil {
				diags.AddError("Failed to encode parameters", err.Error())
				return diags
			}
			m.ParametersJSON = types.StringValue(string(b))
		} else {
			m.ParametersJSON = types.StringNull()
		}
	} else {
		m.ParametersJSON = types.StringNull()
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
