// Package registry_module implements the terrapod_registry_module resource.
//
// API Contract (Terrapod API ↔ Terraform Provider):
//
//	JSON:API type: "registry-modules"
//	ID: UUID (no prefix)
//	Create:  POST   /api/v2/organizations/default/registry-modules
//	Read:    GET    /api/v2/organizations/default/registry-modules/private/default/{name}/{provider}
//	Update:  PATCH  /api/v2/organizations/default/registry-modules/private/default/{name}/{provider}
//	Delete:  DELETE /api/v2/organizations/default/registry-modules/private/default/{name}/{provider}
//
// Attribute mapping:
//
//	"name"               → name               (string, required, forces new)
//	"provider"           → provider_name      (string, required, forces new)
//	"labels"             → labels             (map, optional)
//	"vcs-connection-id"  → vcs_connection_id  (string, optional)
//	"vcs-repo-url"       → vcs_repo_url       (string, optional)
//	"vcs-branch"         → vcs_branch         (string, optional)
//	"vcs-tag-pattern"    → vcs_tag_pattern    (string, optional)
//
// Read-only:
//
//	"namespace"    → namespace    (string, always "default")
//	"status"       → status       (string)
//	"owner-email"  → owner_email  (string)
//	"source"       → source       (string)
//	"created-at"   → created_at   (string)
//	"updated-at"   → updated_at   (string)
//
// Import: name/provider (e.g. "my-module/aws")
package registry_module

import (
	"context"
	"fmt"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

type registryModuleModel struct {
	ID              types.String `tfsdk:"id"`
	Name            types.String `tfsdk:"name"`
	ProviderName    types.String `tfsdk:"provider_name"`
	Labels          types.Map    `tfsdk:"labels"`
	VCSConnectionID types.String `tfsdk:"vcs_connection_id"`
	VCSRepoURL      types.String `tfsdk:"vcs_repo_url"`
	VCSBranch       types.String `tfsdk:"vcs_branch"`
	VCSTagPattern   types.String `tfsdk:"vcs_tag_pattern"`
	Namespace       types.String `tfsdk:"namespace"`
	Status          types.String `tfsdk:"status"`
	OwnerEmail      types.String `tfsdk:"owner_email"`
	Source          types.String `tfsdk:"source"`
	CreatedAt       types.String `tfsdk:"created_at"`
	UpdatedAt       types.String `tfsdk:"updated_at"`
}

var (
	_ resource.Resource                = &registryModuleResource{}
	_ resource.ResourceWithImportState = &registryModuleResource{}
)

type registryModuleResource struct {
	client *client.Client
}

func NewResource() resource.Resource {
	return &registryModuleResource{}
}

func (r *registryModuleResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_registry_module"
}

func (r *registryModuleResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a private module in the Terrapod registry.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed: true, Description: "Module ID.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"name": schema.StringAttribute{
				Required: true, Description: "Module name.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"provider_name": schema.StringAttribute{
				Required: true, Description: "Provider name (e.g. aws, gcp).",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"labels": schema.MapAttribute{
				Optional: true, ElementType: types.StringType,
				Description: "Labels for RBAC evaluation.",
			},
			"vcs_connection_id": schema.StringAttribute{Optional: true, Description: "VCS connection ID."},
			"vcs_repo_url":      schema.StringAttribute{Optional: true, Description: "VCS repo URL."},
			"vcs_branch":        schema.StringAttribute{Optional: true, Description: "VCS branch."},
			"vcs_tag_pattern":   schema.StringAttribute{Optional: true, Description: "VCS tag pattern (e.g. v*)."},
			"namespace":         schema.StringAttribute{Computed: true, Description: "Namespace (always default)."},
			"status":            schema.StringAttribute{Computed: true, Description: "Module status."},
			"owner_email":       schema.StringAttribute{Computed: true, Description: "Owner email."},
			"source":            schema.StringAttribute{Computed: true, Description: "Source (upload or vcs)."},
			"created_at":        schema.StringAttribute{Computed: true, Description: "Creation timestamp."},
			"updated_at":        schema.StringAttribute{Computed: true, Description: "Update timestamp."},
		},
	}
}

func (r *registryModuleResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *registryModuleResource) modulePath(m *registryModuleModel) string {
	return fmt.Sprintf("/api/v2/organizations/default/registry-modules/private/default/%s/%s",
		m.Name.ValueString(), m.ProviderName.ValueString())
}

func (r *registryModuleResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan registryModuleModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs := buildModuleAttrs(&plan)
	body, err := client.MarshalResource("registry-modules", attrs, nil)
	if err != nil {
		resp.Diagnostics.AddError("Marshal error", err.Error())
		return
	}

	data, err := r.client.Post(ctx, "/api/v2/organizations/default/registry-modules", body)
	if err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Parse error", err.Error())
		return
	}

	resp.Diagnostics.Append(readModuleIntoModel(ctx, res, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *registryModuleResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state registryModuleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	data, err := r.client.Get(ctx, r.modulePath(&state))
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

	resp.Diagnostics.Append(readModuleIntoModel(ctx, res, &state)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *registryModuleResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan registryModuleModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs := buildModuleAttrs(&plan)
	body, err := client.MarshalResourceWithID(plan.ID.ValueString(), "registry-modules", attrs)
	if err != nil {
		resp.Diagnostics.AddError("Marshal error", err.Error())
		return
	}

	data, err := r.client.Patch(ctx, r.modulePath(&plan), body)
	if err != nil {
		resp.Diagnostics.AddError("Update failed", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Parse error", err.Error())
		return
	}

	resp.Diagnostics.Append(readModuleIntoModel(ctx, res, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *registryModuleResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state registryModuleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	err := r.client.Delete(ctx, r.modulePath(&state))
	if err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Delete failed", err.Error())
	}
}

func (r *registryModuleResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	parts := strings.SplitN(req.ID, "/", 2)
	if len(parts) != 2 {
		resp.Diagnostics.AddError("Invalid import ID", "Expected format: name/provider")
		return
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("name"), parts[0])...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("provider_name"), parts[1])...)
}

func buildModuleAttrs(m *registryModuleModel) map[string]any {
	attrs := map[string]any{
		"name":     m.Name.ValueString(),
		"provider": m.ProviderName.ValueString(),
	}
	if !m.Labels.IsNull() && !m.Labels.IsUnknown() {
		labels := map[string]string{}
		for k, v := range m.Labels.Elements() {
			labels[k] = v.(types.String).ValueString()
		}
		attrs["labels"] = labels
	}
	if !m.VCSConnectionID.IsNull() {
		attrs["vcs-connection-id"] = m.VCSConnectionID.ValueString()
	}
	if !m.VCSRepoURL.IsNull() {
		attrs["vcs-repo-url"] = m.VCSRepoURL.ValueString()
	}
	if !m.VCSBranch.IsNull() {
		attrs["vcs-branch"] = m.VCSBranch.ValueString()
	}
	if !m.VCSTagPattern.IsNull() {
		attrs["vcs-tag-pattern"] = m.VCSTagPattern.ValueString()
	}
	return attrs
}

func readModuleIntoModel(ctx context.Context, res *client.Resource, m *registryModuleModel) diag.Diagnostics {
	var diags diag.Diagnostics

	m.ID = types.StringValue(res.ID)
	m.Name = types.StringValue(client.GetStringAttr(res, "name"))
	m.ProviderName = types.StringValue(client.GetStringAttr(res, "provider"))
	m.Namespace = types.StringValue(client.GetStringAttr(res, "namespace"))
	m.Status = types.StringValue(client.GetStringAttr(res, "status"))
	m.OwnerEmail = types.StringValue(client.GetStringAttr(res, "owner-email"))
	m.Source = types.StringValue(client.GetStringAttr(res, "source"))
	m.CreatedAt = types.StringValue(client.GetStringAttr(res, "created-at"))
	m.UpdatedAt = types.StringValue(client.GetStringAttr(res, "updated-at"))

	setOptStr(&m.VCSConnectionID, client.GetStringAttr(res, "vcs-connection-id"))
	setOptStr(&m.VCSRepoURL, client.GetStringAttr(res, "vcs-repo-url"))
	setOptStr(&m.VCSBranch, client.GetStringAttr(res, "vcs-branch"))
	setOptStr(&m.VCSTagPattern, client.GetStringAttr(res, "vcs-tag-pattern"))

	if labels := client.GetMapAttr(res, "labels"); len(labels) > 0 {
		val, d := types.MapValueFrom(ctx, types.StringType, labels)
		diags.Append(d...)
		m.Labels = val
	} else {
		m.Labels = types.MapNull(types.StringType)
	}

	return diags
}

func setOptStr(target *types.String, value string) {
	if value != "" {
		*target = types.StringValue(value)
	} else {
		*target = types.StringNull()
	}
}
