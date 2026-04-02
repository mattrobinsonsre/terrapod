// Package registry_provider implements the terrapod_registry_provider resource.
//
// API Contract (Terrapod API ↔ Terraform Provider):
//
//	JSON:API type: "registry-providers"
//	ID: UUID (no prefix)
//	Create:  POST   /api/v2/organizations/default/registry-providers
//	Read:    GET    /api/v2/organizations/default/registry-providers/private/default/{name}
//	Update:  PATCH  /api/v2/organizations/default/registry-providers/private/default/{name}
//	Delete:  DELETE /api/v2/organizations/default/registry-providers/private/default/{name}
//
// Attribute mapping:
//
//	"name"     → name     (string, required, forces new)
//	"labels"   → labels   (map, optional)
//
// Read-only:
//
//	"namespace"    → namespace   (string, always "default")
//	"owner-email"  → owner_email (string)
//	"created-at"   → created_at  (string)
//	"updated-at"   → updated_at  (string)
//
// Import: by name.
package registry_provider

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

type registryProviderModel struct {
	ID         types.String `tfsdk:"id"`
	Name       types.String `tfsdk:"name"`
	Labels     types.Map    `tfsdk:"labels"`
	Namespace  types.String `tfsdk:"namespace"`
	OwnerEmail types.String `tfsdk:"owner_email"`
	CreatedAt  types.String `tfsdk:"created_at"`
	UpdatedAt  types.String `tfsdk:"updated_at"`
}

var (
	_ resource.Resource                = &registryProviderResource{}
	_ resource.ResourceWithImportState = &registryProviderResource{}
)

type registryProviderResource struct {
	client *client.Client
}

func NewResource() resource.Resource {
	return &registryProviderResource{}
}

func (r *registryProviderResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_registry_provider"
}

func (r *registryProviderResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a private provider in the Terrapod registry.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed: true, Description: "Provider ID.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"name": schema.StringAttribute{
				Required: true, Description: "Provider name.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"labels": schema.MapAttribute{
				Optional: true, ElementType: types.StringType,
				Description: "Labels for RBAC evaluation.",
			},
			"namespace": schema.StringAttribute{
				Computed: true, Description: "Namespace (always default).",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"owner_email": schema.StringAttribute{
				Computed: true, Description: "Owner email.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"created_at": schema.StringAttribute{
				Computed: true, Description: "Creation timestamp.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"updated_at": schema.StringAttribute{
				Computed: true, Description: "Update timestamp.",
			},
		},
	}
}

func (r *registryProviderResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	c, ok := req.ProviderData.(*client.Client)
	if !ok {
		resp.Diagnostics.AddError("Unexpected provider data type", fmt.Sprintf("Expected *client.Client, got %T", req.ProviderData))
		return
	}
	r.client = c
}

func providerPath(name string) string {
	return fmt.Sprintf("/api/v2/organizations/default/registry-providers/private/default/%s", name)
}

func (r *registryProviderResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan registryProviderModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs := buildProviderAttrs(&plan)
	body, err := client.MarshalResource("registry-providers", attrs, nil)
	if err != nil {
		resp.Diagnostics.AddError("Marshal error", err.Error())
		return
	}

	data, err := r.client.Post(ctx, "/api/v2/organizations/default/registry-providers", body)
	if err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Parse error", err.Error())
		return
	}

	resp.Diagnostics.Append(readProviderIntoModel(ctx, res, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *registryProviderResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state registryProviderModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	data, err := r.client.Get(ctx, providerPath(state.Name.ValueString()))
	if err != nil {
		if client.IsNotFound(err) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Parse error", err.Error())
		return
	}

	resp.Diagnostics.Append(readProviderIntoModel(ctx, res, &state)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *registryProviderResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan registryProviderModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs := buildProviderAttrs(&plan)
	body, err := client.MarshalResourceWithID(plan.ID.ValueString(), "registry-providers", attrs)
	if err != nil {
		resp.Diagnostics.AddError("Marshal error", err.Error())
		return
	}

	data, err := r.client.Patch(ctx, providerPath(plan.Name.ValueString()), body)
	if err != nil {
		resp.Diagnostics.AddError("Update failed", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Parse error", err.Error())
		return
	}

	resp.Diagnostics.Append(readProviderIntoModel(ctx, res, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *registryProviderResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state registryProviderModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	err := r.client.Delete(ctx, providerPath(state.Name.ValueString()))
	if err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Delete failed", err.Error())
	}
}

func (r *registryProviderResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("name"), req.ID)...)
}

func buildProviderAttrs(m *registryProviderModel) map[string]any {
	attrs := map[string]any{
		"name": m.Name.ValueString(),
	}
	if !m.Labels.IsNull() && !m.Labels.IsUnknown() {
		labels := map[string]string{}
		for k, v := range m.Labels.Elements() {
			labels[k] = v.(types.String).ValueString()
		}
		attrs["labels"] = labels
	}
	return attrs
}

func readProviderIntoModel(ctx context.Context, res *client.Resource, m *registryProviderModel) diag.Diagnostics {
	var diags diag.Diagnostics

	m.ID = types.StringValue(res.ID)
	m.Name = types.StringValue(client.GetStringAttr(res, "name"))
	m.Namespace = types.StringValue(client.GetStringAttr(res, "namespace"))
	m.OwnerEmail = types.StringValue(client.GetStringAttr(res, "owner-email"))
	m.CreatedAt = types.StringValue(client.GetStringAttr(res, "created-at"))
	m.UpdatedAt = types.StringValue(client.GetStringAttr(res, "updated-at"))

	if labels := client.GetMapAttr(res, "labels"); len(labels) > 0 {
		val, d := types.MapValueFrom(ctx, types.StringType, labels)
		diags.Append(d...)
		m.Labels = val
	} else {
		m.Labels = types.MapNull(types.StringType)
	}

	return diags
}
