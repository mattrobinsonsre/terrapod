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
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
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
	Subdirectory    types.String `tfsdk:"subdirectory"`
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
			"id":                schema.StringAttribute{Computed: true, Description: "Module ID.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"name":              schema.StringAttribute{Required: true, Description: "Module name.", PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()}},
			"provider_name":     schema.StringAttribute{Required: true, Description: "Provider name (e.g. aws, gcp).", PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()}},
			"labels":            schema.MapAttribute{Optional: true, ElementType: types.StringType, Description: "Labels for RBAC evaluation."},
			"vcs_connection_id": schema.StringAttribute{Optional: true, Description: "VCS connection ID."},
			"vcs_repo_url":      schema.StringAttribute{Optional: true, Description: "VCS repo URL."},
			"vcs_branch":        schema.StringAttribute{Optional: true, Description: "VCS branch."},
			// Optional+Computed with a default: the server stores "v*" when none is
			// given, so an Optional-only attribute left out of configuration came
			// back as "v*" and failed the apply as an inconsistent result (#1634).
			"vcs_tag_pattern": schema.StringAttribute{
				Optional:    true,
				Computed:    true,
				Default:     stringdefault.StaticString("v*"),
				Description: "VCS tag pattern (e.g. v*). Defaults to \"v*\"; an empty string means the same.",
			},
			// Optional+Computed with an empty default rather than UseStateForUnknown:
			// removing the attribute from configuration must clear the subdirectory,
			// so the plan has to say "" and the update has to send it (#1634).
			"subdirectory": schema.StringAttribute{
				Optional: true,
				Computed: true,
				Default:  stringdefault.StaticString(""),
				Description: "Path within the repository to publish the module from, for a submodule (e.g. modules/create). " +
					"Requires vcs_repo_url; omit (or set \"\") for a module at the repository root. Surrounding whitespace " +
					"and leading or trailing slashes are ignored.",
			},
			"namespace":   schema.StringAttribute{Computed: true, Description: "Namespace (always default).", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"status":      schema.StringAttribute{Computed: true, Description: "Module status."},
			"owner_email": schema.StringAttribute{Computed: true, Description: "Owner email.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"source":      schema.StringAttribute{Computed: true, Description: "Source (upload or vcs).", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"created_at":  schema.StringAttribute{Computed: true, Description: "Creation timestamp.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"updated_at":  schema.StringAttribute{Computed: true, Description: "Update timestamp."},
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
	if !plan.VCSTagPattern.IsNull() && !plan.VCSTagPattern.IsUnknown() {
		sdkReq.VCSTagPattern = plan.VCSTagPattern.ValueString()
	}
	if !plan.Subdirectory.IsNull() && !plan.Subdirectory.IsUnknown() {
		sdkReq.Subdirectory = plan.Subdirectory.ValueString()
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
	m, err := r.tc.UpdateRegistryModule(ctx, plan.Name.ValueString(), plan.ProviderName.ValueString(), updateRequestFromPlan(&plan))
	if err != nil {
		resp.Diagnostics.AddError("Update failed", err.Error())
		return
	}
	resp.Diagnostics.Append(readModuleFromSDK(ctx, m, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

// updateRequestFromPlan builds the PATCH body. subdirectory and vcs_tag_pattern
// have defaults, so they are always known and always sent: an empty
// subdirectory is how removing it from configuration clears it on the server.
func updateRequestFromPlan(plan *registryModuleModel) terrapod.UpdateRegistryModuleRequest {
	sdkReq := terrapod.UpdateRegistryModuleRequest{}
	if !plan.Labels.IsNull() && !plan.Labels.IsUnknown() {
		labels := map[string]string{}
		for k, v := range plan.Labels.Elements() {
			labels[k] = v.(types.String).ValueString()
		}
		sdkReq.Labels = &labels
	}
	str := func(v types.String) *string {
		if v.IsNull() || v.IsUnknown() {
			return nil
		}
		s := v.ValueString()
		return &s
	}
	sdkReq.VCSConnectionID = str(plan.VCSConnectionID)
	sdkReq.VCSRepoURL = str(plan.VCSRepoURL)
	sdkReq.VCSBranch = str(plan.VCSBranch)
	sdkReq.VCSTagPattern = str(plan.VCSTagPattern)
	sdkReq.Subdirectory = str(plan.Subdirectory)
	return sdkReq
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

// normalizeSubdirectory mirrors the server's normalize_subdirectory for the
// forms it accepts: surrounding whitespace, then leading and trailing "/", are
// dropped. The server still validates the rest.
func normalizeSubdirectory(s string) string {
	return strings.Trim(strings.TrimSpace(s), "/")
}

// normalizeTagPattern mirrors the server: an empty tag pattern means "v*".
func normalizeTagPattern(s string) string {
	if s == "" {
		return "v*"
	}
	return s
}

func normalizeConnection(s string) string { return strings.TrimPrefix(s, "vcs-") }

func identity(s string) string { return s }

// keepConfigured reports whether the value already in the model (the plan on
// Create/Update, the prior state on Read) names what the server returned, once
// both are normalised the way the server normalises. Keeping it then avoids
// "Provider produced inconsistent result after apply" for a value the server
// merely tidied, and a perpetual diff on the next plan.
func keepConfigured(current types.String, server string, norm func(string) string) bool {
	return !current.IsNull() && !current.IsUnknown() && norm(current.ValueString()) == norm(server)
}

// setOpt sets an Optional-only attribute: the configured form when it is
// equivalent to the server's, otherwise the server's value with "" as null.
func setOpt(target *types.String, server string, norm func(string) string) {
	if keepConfigured(*target, server, norm) {
		return
	}
	setOptStr(target, server)
}

// setComputed sets an Optional+Computed attribute: the configured form when it
// is equivalent to the server's, otherwise the server's value, "" included.
func setComputed(target *types.String, server string, norm func(string) string) {
	if keepConfigured(*target, server, norm) {
		return
	}
	*target = types.StringValue(server)
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
	setOpt(&mod.VCSConnectionID, m.VCSConnectionID, normalizeConnection)
	setOpt(&mod.VCSRepoURL, m.VCSRepoURL, identity)
	setOpt(&mod.VCSBranch, m.VCSBranch, identity)
	setComputed(&mod.VCSTagPattern, normalizeTagPattern(m.VCSTagPattern), normalizeTagPattern)
	setComputed(&mod.Subdirectory, m.Subdirectory, normalizeSubdirectory)
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
