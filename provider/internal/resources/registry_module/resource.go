// Package registry_module — migrated to go-terrapod (#347).
package registry_module

import (
	"context"
	"errors"
	"fmt"
	"strings"

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
	tc     *terrapod.Client
}

func NewResource() resource.Resource { return &registryModuleResource{} }

func (r *registryModuleResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_registry_module"
}

func (r *registryModuleResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a private module in the Terrapod registry.",
		Attributes: map[string]schema.Attribute{
			"id":   schema.StringAttribute{Computed: true, Description: "Module ID.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"name": schema.StringAttribute{Required: true, Description: "Module name.", PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()}},
			"provider_name": schema.StringAttribute{Required: true, Description: "Provider name (e.g. aws, gcp).", PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()}},
			"labels":            schema.MapAttribute{Optional: true, ElementType: types.StringType, Description: "Labels for RBAC evaluation."},
			"vcs_connection_id": schema.StringAttribute{Optional: true, Description: "VCS connection ID."},
			"vcs_repo_url":      schema.StringAttribute{Optional: true, Description: "VCS repo URL."},
			"vcs_branch":        schema.StringAttribute{Optional: true, Description: "VCS branch."},
			"vcs_tag_pattern":   schema.StringAttribute{Optional: true, Description: "VCS tag pattern (e.g. v*)."},
			"namespace":         schema.StringAttribute{Computed: true, Description: "Namespace (always default).", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"status":            schema.StringAttribute{Computed: true, Description: "Module status."},
			"owner_email":       schema.StringAttribute{Computed: true, Description: "Owner email.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"source":            schema.StringAttribute{Computed: true, Description: "Source (upload or vcs).", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"created_at":        schema.StringAttribute{Computed: true, Description: "Creation timestamp.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
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
	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: c.BaseURL, Token: c.Token})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	r.tc = tc
}

func (r *registryModuleResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan registryModuleModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	sdkReq := terrapod.CreateRegistryModuleRequest{
		Name:         plan.Name.ValueString(),
		ProviderName: plan.ProviderName.ValueString(),
	}
	if !plan.Labels.IsNull() && !plan.Labels.IsUnknown() {
		labels := map[string]string{}
		for k, v := range plan.Labels.Elements() {
			labels[k] = v.(types.String).ValueString()
		}
		sdkReq.Labels = labels
	}
	if !plan.VCSConnectionID.IsNull() {
		sdkReq.VCSConnectionID = plan.VCSConnectionID.ValueString()
	}
	if !plan.VCSRepoURL.IsNull() {
		sdkReq.VCSRepoURL = plan.VCSRepoURL.ValueString()
	}
	if !plan.VCSBranch.IsNull() {
		sdkReq.VCSBranch = plan.VCSBranch.ValueString()
	}
	if !plan.VCSTagPattern.IsNull() {
		sdkReq.VCSTagPattern = plan.VCSTagPattern.ValueString()
	}

	m, err := r.tc.CreateRegistryModule(ctx, sdkReq)
	if err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}
	resp.Diagnostics.Append(readModuleFromSDK(ctx, m, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *registryModuleResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state registryModuleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	m, err := r.tc.GetRegistryModule(ctx, state.Name.ValueString(), state.ProviderName.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}
	resp.Diagnostics.Append(readModuleFromSDK(ctx, m, &state)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *registryModuleResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan registryModuleModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	sdkReq := terrapod.UpdateRegistryModuleRequest{}
	if !plan.Labels.IsNull() && !plan.Labels.IsUnknown() {
		labels := map[string]string{}
		for k, v := range plan.Labels.Elements() {
			labels[k] = v.(types.String).ValueString()
		}
		sdkReq.Labels = &labels
	}
	if !plan.VCSConnectionID.IsNull() && !plan.VCSConnectionID.IsUnknown() {
		s := plan.VCSConnectionID.ValueString()
		sdkReq.VCSConnectionID = &s
	}
	if !plan.VCSRepoURL.IsNull() && !plan.VCSRepoURL.IsUnknown() {
		s := plan.VCSRepoURL.ValueString()
		sdkReq.VCSRepoURL = &s
	}
	if !plan.VCSBranch.IsNull() && !plan.VCSBranch.IsUnknown() {
		s := plan.VCSBranch.ValueString()
		sdkReq.VCSBranch = &s
	}
	if !plan.VCSTagPattern.IsNull() && !plan.VCSTagPattern.IsUnknown() {
		s := plan.VCSTagPattern.ValueString()
		sdkReq.VCSTagPattern = &s
	}

	m, err := r.tc.UpdateRegistryModule(ctx, plan.Name.ValueString(), plan.ProviderName.ValueString(), sdkReq)
	if err != nil {
		resp.Diagnostics.AddError("Update failed", err.Error())
		return
	}
	resp.Diagnostics.Append(readModuleFromSDK(ctx, m, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *registryModuleResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state registryModuleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	err := r.tc.DeleteRegistryModule(ctx, state.Name.ValueString(), state.ProviderName.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Delete failed", err.Error())
		}
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

func readModuleFromSDK(ctx context.Context, m *terrapod.RegistryModule, mod *registryModuleModel) diag.Diagnostics {
	var diags diag.Diagnostics
	mod.ID = types.StringValue(m.ID)
	mod.Name = types.StringValue(m.Name)
	mod.ProviderName = types.StringValue(m.ProviderName)
	mod.Namespace = types.StringValue(m.Namespace)
	mod.Status = types.StringValue(m.Status)
	mod.OwnerEmail = types.StringValue(m.OwnerEmail)
	mod.Source = types.StringValue(m.Source)
	mod.CreatedAt = types.StringValue(m.CreatedAt)
	mod.UpdatedAt = types.StringValue(m.UpdatedAt)
	setOptStr(&mod.VCSConnectionID, m.VCSConnectionID)
	setOptStr(&mod.VCSRepoURL, m.VCSRepoURL)
	setOptStr(&mod.VCSBranch, m.VCSBranch)
	setOptStr(&mod.VCSTagPattern, m.VCSTagPattern)
	if len(m.Labels) > 0 {
		val, d := types.MapValueFrom(ctx, types.StringType, m.Labels)
		diags.Append(d...)
		mod.Labels = val
	} else {
		mod.Labels = types.MapNull(types.StringType)
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
