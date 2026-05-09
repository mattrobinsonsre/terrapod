// Package autodiscovery_rule implements the terrapod_autodiscovery_rule
// resource. See terrapod #283 and docs/autodiscovery.md.
//
// API Contract (Terrapod API <-> Terraform Provider):
//
//	JSON:API type: "autodiscovery-rules"
//	ID format: bare UUID (no prefix)
//	Create:  POST   /api/terrapod/v1/autodiscovery-rules
//	Read:    GET    /api/terrapod/v1/autodiscovery-rules/{id}
//	Update:  PATCH  /api/terrapod/v1/autodiscovery-rules/{id}
//	Delete:  DELETE /api/terrapod/v1/autodiscovery-rules/{id}
//
// Attribute mapping (JSON:API attribute -> Terraform schema attribute):
//
//	"name"               -> name                (string, required)
//	"name-template"      -> name_template       (string, optional)
//	"vcs-connection-id"  -> vcs_connection_id   (string, required)
//	"repo-url"           -> repo_url            (string, required)
//	"branch"             -> branch              (string, optional, "" = default branch)
//	"pattern"            -> pattern             (string, required)
//	"ignore-patterns"    -> ignore_patterns     (list of strings, optional)
//	"enabled"            -> enabled             (bool, optional, default true)
//	"execution-mode"     -> execution_mode      (string, optional, default "agent")
//	"execution-backend"  -> execution_backend   (string, optional, default "tofu")
//	"agent-pool-id"      -> agent_pool_id       (string, optional)
//	"terraform-version"  -> terraform_version   (string, optional, default "1.11")
//	"resource-cpu"       -> resource_cpu        (string, optional, default "1")
//	"resource-memory"    -> resource_memory     (string, optional, default "2Gi")
//	"auto-apply"         -> auto_apply          (bool, optional, default false)
//	"labels"             -> labels              (map[string]string, optional)
//	"owner-email"        -> owner_email         (string, optional)
//
// Read-only:
//
//	"created-at"         -> created_at          (string, computed)
//	"updated-at"         -> updated_at          (string, computed)
//
// Import: by rule ID (bare UUID).
package autodiscovery_rule

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/boolplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/listplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/mapplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var (
	_ resource.Resource                = &autodiscoveryRuleResource{}
	_ resource.ResourceWithImportState = &autodiscoveryRuleResource{}
)

type autodiscoveryRuleModel struct {
	ID types.String `tfsdk:"id"`

	Name             types.String `tfsdk:"name"`
	NameTemplate     types.String `tfsdk:"name_template"`
	VCSConnectionID  types.String `tfsdk:"vcs_connection_id"`
	RepoURL          types.String `tfsdk:"repo_url"`
	Branch           types.String `tfsdk:"branch"`
	Pattern          types.String `tfsdk:"pattern"`
	IgnorePatterns   types.List   `tfsdk:"ignore_patterns"`
	Enabled          types.Bool   `tfsdk:"enabled"`
	ExecutionMode    types.String `tfsdk:"execution_mode"`
	ExecutionBackend types.String `tfsdk:"execution_backend"`
	AgentPoolID      types.String `tfsdk:"agent_pool_id"`
	TerraformVersion types.String `tfsdk:"terraform_version"`
	ResourceCPU      types.String `tfsdk:"resource_cpu"`
	ResourceMemory   types.String `tfsdk:"resource_memory"`
	AutoApply        types.Bool   `tfsdk:"auto_apply"`
	Labels           types.Map    `tfsdk:"labels"`
	OwnerEmail       types.String `tfsdk:"owner_email"`

	CreatedAt types.String `tfsdk:"created_at"`
	UpdatedAt types.String `tfsdk:"updated_at"`
}

type autodiscoveryRuleResource struct {
	client *client.Client
}

func NewResource() resource.Resource {
	return &autodiscoveryRuleResource{}
}

func (r *autodiscoveryRuleResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_autodiscovery_rule"
}

func (r *autodiscoveryRuleResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a Terrapod workspace autodiscovery rule. " +
			"When the VCS poller detects a PR or default-branch push that touches " +
			"a path matching this rule's pattern (and not matching ignore_patterns), " +
			"it auto-creates a workspace using the rule's template fields. " +
			"See https://github.com/mattrobinsonsre/terrapod/blob/main/docs/autodiscovery.md.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description: "The autodiscovery rule ID (UUID).",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"name": schema.StringAttribute{
				Description: "Display name for the rule. Unique per VCS connection.",
				Required:    true,
			},
			"name_template": schema.StringAttribute{
				Description: "Optional template for derived workspace names. Use {path} for the dashed working_directory or {root} to keep slashes. Defaults to {path}.",
				Optional:    true,
				Computed:    true,
				Default:     stringdefault.StaticString(""),
			},
			"vcs_connection_id": schema.StringAttribute{
				Description: "The VCS connection ID this rule scopes to (e.g. \"vcs-abc123\").",
				Required:    true,
			},
			"repo_url": schema.StringAttribute{
				Description: "Full repository URL (e.g. https://github.com/myorg/monorepo).",
				Required:    true,
			},
			"branch": schema.StringAttribute{
				Description: "Branch the rule scopes to. Empty (default) tracks the repo's default branch.",
				Optional:    true,
				Computed:    true,
				Default:     stringdefault.StaticString(""),
			},
			"pattern": schema.StringAttribute{
				Description: "Glob matched against changed file paths (gitignore-style with ** support).",
				Required:    true,
			},
			"ignore_patterns": schema.ListAttribute{
				Description: "Globs filtered out before pattern matching.",
				Optional:    true,
				Computed:    true,
				ElementType: types.StringType,
				PlanModifiers: []planmodifier.List{
					listplanmodifier.UseStateForUnknown(),
				},
			},
			"enabled": schema.BoolAttribute{
				Description: "Whether the rule is active. Defaults to true.",
				Optional:    true,
				Computed:    true,
				Default:     booldefault.StaticBool(true),
				PlanModifiers: []planmodifier.Bool{
					boolplanmodifier.UseStateForUnknown(),
				},
			},
			"execution_mode": schema.StringAttribute{
				Description: "Default execution mode for created workspaces (\"agent\" or \"local\"). Defaults to \"agent\".",
				Optional:    true,
				Computed:    true,
				Default:     stringdefault.StaticString("agent"),
			},
			"execution_backend": schema.StringAttribute{
				Description: "Default execution backend for created workspaces (\"tofu\" or \"terraform\"). Defaults to \"tofu\".",
				Optional:    true,
				Computed:    true,
				Default:     stringdefault.StaticString("tofu"),
			},
			"agent_pool_id": schema.StringAttribute{
				Description: "Default agent pool ID for created workspaces (e.g. \"apool-abc123\"). Optional.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"terraform_version": schema.StringAttribute{
				Description: "Default terraform/tofu version for created workspaces. Defaults to \"1.11\".",
				Optional:    true,
				Computed:    true,
				Default:     stringdefault.StaticString("1.11"),
			},
			"resource_cpu": schema.StringAttribute{
				Description: "Default CPU request for runner Jobs in created workspaces. Defaults to \"1\".",
				Optional:    true,
				Computed:    true,
				Default:     stringdefault.StaticString("1"),
			},
			"resource_memory": schema.StringAttribute{
				Description: "Default memory request for runner Jobs in created workspaces. Defaults to \"2Gi\".",
				Optional:    true,
				Computed:    true,
				Default:     stringdefault.StaticString("2Gi"),
			},
			"auto_apply": schema.BoolAttribute{
				Description: "Default auto-apply setting for created workspaces. Defaults to false.",
				Optional:    true,
				Computed:    true,
				Default:     booldefault.StaticBool(false),
				PlanModifiers: []planmodifier.Bool{
					boolplanmodifier.UseStateForUnknown(),
				},
			},
			"labels": schema.MapAttribute{
				Description: "Labels inherited by created workspaces — feeds Terrapod's label-based RBAC and filtering.",
				Optional:    true,
				Computed:    true,
				ElementType: types.StringType,
				PlanModifiers: []planmodifier.Map{
					mapplanmodifier.UseStateForUnknown(),
				},
			},
			"owner_email": schema.StringAttribute{
				Description: "Email used as owner_email on created workspaces. Empty = no owner; label-RBAC alone determines access.",
				Optional:    true,
				Computed:    true,
				Default:     stringdefault.StaticString(""),
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

func (r *autodiscoveryRuleResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *autodiscoveryRuleResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan autodiscoveryRuleModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs := buildAutodiscoveryRuleAttrs(&plan)

	body, err := client.MarshalResource("autodiscovery-rules", attrs, nil)
	if err != nil {
		resp.Diagnostics.AddError("Failed to marshal request", err.Error())
		return
	}

	data, err := r.client.Post(ctx, "/api/terrapod/v1/autodiscovery-rules", body)
	if err != nil {
		resp.Diagnostics.AddError("Failed to create autodiscovery rule", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	resp.Diagnostics.Append(readAutodiscoveryRuleIntoModel(ctx, res, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *autodiscoveryRuleResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state autodiscoveryRuleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	data, err := r.client.Get(ctx, "/api/terrapod/v1/autodiscovery-rules/"+state.ID.ValueString())
	if err != nil {
		if client.IsNotFound(err) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Failed to read autodiscovery rule", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	resp.Diagnostics.Append(readAutodiscoveryRuleIntoModel(ctx, res, &state)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *autodiscoveryRuleResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan autodiscoveryRuleModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var state autodiscoveryRuleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs := buildAutodiscoveryRuleAttrs(&plan)

	body, err := client.MarshalResourceWithID(state.ID.ValueString(), "autodiscovery-rules", attrs)
	if err != nil {
		resp.Diagnostics.AddError("Failed to marshal request", err.Error())
		return
	}

	data, err := r.client.Patch(ctx, "/api/terrapod/v1/autodiscovery-rules/"+state.ID.ValueString(), body)
	if err != nil {
		resp.Diagnostics.AddError("Failed to update autodiscovery rule", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	resp.Diagnostics.Append(readAutodiscoveryRuleIntoModel(ctx, res, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *autodiscoveryRuleResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state autodiscoveryRuleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	err := r.client.Delete(ctx, "/api/terrapod/v1/autodiscovery-rules/"+state.ID.ValueString())
	if err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Failed to delete autodiscovery rule", err.Error())
	}
}

func (r *autodiscoveryRuleResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

// buildAutodiscoveryRuleAttrs converts the Terraform model into JSON:API
// attributes for create/update. Computed-only attributes are omitted.
func buildAutodiscoveryRuleAttrs(m *autodiscoveryRuleModel) map[string]any {
	attrs := map[string]any{
		"name":              m.Name.ValueString(),
		"vcs-connection-id": m.VCSConnectionID.ValueString(),
		"repo-url":          m.RepoURL.ValueString(),
		"pattern":           m.Pattern.ValueString(),
	}

	if !m.NameTemplate.IsNull() && !m.NameTemplate.IsUnknown() {
		attrs["name-template"] = m.NameTemplate.ValueString()
	}
	if !m.Branch.IsNull() && !m.Branch.IsUnknown() {
		attrs["branch"] = m.Branch.ValueString()
	}

	if !m.IgnorePatterns.IsNull() && !m.IgnorePatterns.IsUnknown() {
		patterns := make([]string, 0, len(m.IgnorePatterns.Elements()))
		for _, v := range m.IgnorePatterns.Elements() {
			patterns = append(patterns, v.(types.String).ValueString())
		}
		attrs["ignore-patterns"] = patterns
	} else {
		attrs["ignore-patterns"] = []string{}
	}

	if !m.Enabled.IsNull() && !m.Enabled.IsUnknown() {
		attrs["enabled"] = m.Enabled.ValueBool()
	}
	if !m.ExecutionMode.IsNull() && !m.ExecutionMode.IsUnknown() {
		attrs["execution-mode"] = m.ExecutionMode.ValueString()
	}
	if !m.ExecutionBackend.IsNull() && !m.ExecutionBackend.IsUnknown() {
		attrs["execution-backend"] = m.ExecutionBackend.ValueString()
	}
	if !m.AgentPoolID.IsNull() && !m.AgentPoolID.IsUnknown() {
		v := m.AgentPoolID.ValueString()
		if v == "" {
			attrs["agent-pool-id"] = nil
		} else {
			attrs["agent-pool-id"] = v
		}
	}
	if !m.TerraformVersion.IsNull() && !m.TerraformVersion.IsUnknown() {
		attrs["terraform-version"] = m.TerraformVersion.ValueString()
	}
	if !m.ResourceCPU.IsNull() && !m.ResourceCPU.IsUnknown() {
		attrs["resource-cpu"] = m.ResourceCPU.ValueString()
	}
	if !m.ResourceMemory.IsNull() && !m.ResourceMemory.IsUnknown() {
		attrs["resource-memory"] = m.ResourceMemory.ValueString()
	}
	if !m.AutoApply.IsNull() && !m.AutoApply.IsUnknown() {
		attrs["auto-apply"] = m.AutoApply.ValueBool()
	}

	if !m.Labels.IsNull() && !m.Labels.IsUnknown() {
		labels := map[string]string{}
		for k, v := range m.Labels.Elements() {
			labels[k] = v.(types.String).ValueString()
		}
		attrs["labels"] = labels
	} else {
		attrs["labels"] = map[string]string{}
	}

	if !m.OwnerEmail.IsNull() && !m.OwnerEmail.IsUnknown() {
		attrs["owner-email"] = m.OwnerEmail.ValueString()
	}

	return attrs
}

// readAutodiscoveryRuleIntoModel maps a JSON:API resource into the
// Terraform model.
func readAutodiscoveryRuleIntoModel(ctx context.Context, res *client.Resource, m *autodiscoveryRuleModel) diag.Diagnostics {
	var diags diag.Diagnostics

	m.ID = types.StringValue(res.ID)

	m.Name = types.StringValue(client.GetStringAttr(res, "name"))
	m.NameTemplate = types.StringValue(client.GetStringAttr(res, "name-template"))
	m.VCSConnectionID = types.StringValue(client.GetStringAttr(res, "vcs-connection-id"))
	m.RepoURL = types.StringValue(client.GetStringAttr(res, "repo-url"))
	m.Branch = types.StringValue(client.GetStringAttr(res, "branch"))
	m.Pattern = types.StringValue(client.GetStringAttr(res, "pattern"))

	patterns := client.GetListAttr(res, "ignore-patterns")
	if patterns == nil {
		patterns = []string{}
	}
	v, d := types.ListValueFrom(ctx, types.StringType, patterns)
	diags.Append(d...)
	m.IgnorePatterns = v

	m.Enabled = types.BoolValue(client.GetBoolAttr(res, "enabled"))
	m.ExecutionMode = types.StringValue(client.GetStringAttr(res, "execution-mode"))
	m.ExecutionBackend = types.StringValue(client.GetStringAttr(res, "execution-backend"))
	m.AgentPoolID = types.StringValue(client.GetStringAttr(res, "agent-pool-id"))
	m.TerraformVersion = types.StringValue(client.GetStringAttr(res, "terraform-version"))
	m.ResourceCPU = types.StringValue(client.GetStringAttr(res, "resource-cpu"))
	m.ResourceMemory = types.StringValue(client.GetStringAttr(res, "resource-memory"))
	m.AutoApply = types.BoolValue(client.GetBoolAttr(res, "auto-apply"))

	labels := client.GetMapAttr(res, "labels")
	if labels == nil {
		labels = map[string]string{}
	}
	mv, dl := types.MapValueFrom(ctx, types.StringType, labels)
	diags.Append(dl...)
	m.Labels = mv

	m.OwnerEmail = types.StringValue(client.GetStringAttr(res, "owner-email"))
	m.CreatedAt = types.StringValue(client.GetStringAttr(res, "created-at"))
	m.UpdatedAt = types.StringValue(client.GetStringAttr(res, "updated-at"))

	return diags
}
