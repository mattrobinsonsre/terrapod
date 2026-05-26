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
//	"on-directory-delete" -> on_directory_delete (string, optional;
//	    "flag" (default, safe) or "destroy" (tears down infra then archives))
//	"var-files"          -> var_files           (list of strings, optional)
//	"run-task-templates" -> run_task_templates  (list of objects, optional)
//	    each: name (string, req), url (string, req), hmac-key (string, opt),
//	          stage (string, req), enforcement-level (string, opt),
//	          enabled (bool, opt)
//	"notification-templates" -> notification_templates (list of objects, optional)
//	    each: name (string, req), destination-type (string, req),
//	          url (string, opt), token (string, opt),
//	          triggers (list of strings, opt),
//	          email-addresses (list of strings, opt), enabled (bool, opt)
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
	"encoding/json"
	"errors"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/attr"
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

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var (
	_ resource.Resource                = &autodiscoveryRuleResource{}
	_ resource.ResourceWithImportState = &autodiscoveryRuleResource{}
)

type autodiscoveryRuleModel struct {
	ID types.String `tfsdk:"id"`

	Name              types.String `tfsdk:"name"`
	NameTemplate      types.String `tfsdk:"name_template"`
	VCSConnectionID   types.String `tfsdk:"vcs_connection_id"`
	RepoURL           types.String `tfsdk:"repo_url"`
	Branch            types.String `tfsdk:"branch"`
	Pattern           types.String `tfsdk:"pattern"`
	IgnorePatterns    types.List   `tfsdk:"ignore_patterns"`
	Enabled           types.Bool   `tfsdk:"enabled"`
	ExecutionMode     types.String `tfsdk:"execution_mode"`
	ExecutionBackend  types.String `tfsdk:"execution_backend"`
	AgentPoolID       types.String `tfsdk:"agent_pool_id"`
	TerraformVersion  types.String `tfsdk:"terraform_version"`
	ResourceCPU       types.String `tfsdk:"resource_cpu"`
	ResourceMemory    types.String `tfsdk:"resource_memory"`
	AutoApply         types.Bool   `tfsdk:"auto_apply"`
	Labels            types.Map    `tfsdk:"labels"`
	OwnerEmail        types.String `tfsdk:"owner_email"`
	OnDirectoryDelete types.String `tfsdk:"on_directory_delete"`

	VarFiles              types.List `tfsdk:"var_files"`
	RunTaskTemplates      types.List `tfsdk:"run_task_templates"`
	NotificationTemplates types.List `tfsdk:"notification_templates"`

	CreatedAt types.String `tfsdk:"created_at"`
	UpdatedAt types.String `tfsdk:"updated_at"`
}

// runTaskTemplateAttrTypes is the object attribute type map for a single
// element of run_task_templates. Used for nested list (un)marshalling.
var runTaskTemplateAttrTypes = map[string]attr.Type{
	"name":              types.StringType,
	"url":               types.StringType,
	"hmac_key":          types.StringType,
	"stage":             types.StringType,
	"enforcement_level": types.StringType,
	"enabled":           types.BoolType,
}

// notificationTemplateAttrTypes is the object attribute type map for a
// single element of notification_templates.
var notificationTemplateAttrTypes = map[string]attr.Type{
	"name":             types.StringType,
	"destination_type": types.StringType,
	"url":              types.StringType,
	"token":            types.StringType,
	"triggers":         types.ListType{ElemType: types.StringType},
	"email_addresses":  types.ListType{ElemType: types.StringType},
	"enabled":          types.BoolType,
}

type autodiscoveryRuleResource struct {
	client *client.Client
	tc     *terrapod.Client
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
			"on_directory_delete": schema.StringAttribute{
				Description: "What to do with a discovered workspace when its source directory is deleted from the repository. " +
					"\"flag\" (default, safe) marks the workspace and leaves infrastructure intact; " +
					"\"destroy\" (opt-in) tears down the infrastructure then archives the workspace. " +
					"Only \"flag\" or \"destroy\" are accepted; the server rejects other values with HTTP 422.",
				Optional: true,
			},

			"var_files": schema.ListAttribute{
				Description: "List of .tfvars file paths inherited by created workspaces as -var-file arguments.",
				Optional:    true,
				ElementType: types.StringType,
			},
			"run_task_templates": schema.ListNestedAttribute{
				Description: "Run task definitions auto-applied to workspaces created by this rule.",
				Optional:    true,
				NestedObject: schema.NestedAttributeObject{
					Attributes: map[string]schema.Attribute{
						"name": schema.StringAttribute{
							Description: "Run task name.",
							Required:    true,
						},
						"url": schema.StringAttribute{
							Description: "Run task webhook URL.",
							Required:    true,
						},
						"hmac_key": schema.StringAttribute{
							Description: "Optional HMAC signing key for the run task webhook.",
							Optional:    true,
							Sensitive:   true,
						},
						"stage": schema.StringAttribute{
							Description: "Run stage the task runs at (e.g. pre_plan, post_plan, pre_apply).",
							Required:    true,
						},
						"enforcement_level": schema.StringAttribute{
							Description: "Enforcement level: mandatory or advisory. Defaults to mandatory.",
							Optional:    true,
						},
						"enabled": schema.BoolAttribute{
							Description: "Whether the run task is enabled. Defaults to true.",
							Optional:    true,
						},
					},
				},
			},
			"notification_templates": schema.ListNestedAttribute{
				Description: "Notification configurations auto-applied to workspaces created by this rule.",
				Optional:    true,
				NestedObject: schema.NestedAttributeObject{
					Attributes: map[string]schema.Attribute{
						"name": schema.StringAttribute{
							Description: "Notification name.",
							Required:    true,
						},
						"destination_type": schema.StringAttribute{
							Description: "Destination type: generic, slack, or email.",
							Required:    true,
						},
						"url": schema.StringAttribute{
							Description: "Webhook URL (for generic/slack destination types).",
							Optional:    true,
						},
						"token": schema.StringAttribute{
							Description: "Optional HMAC or auth token.",
							Optional:    true,
							Sensitive:   true,
						},
						"triggers": schema.ListAttribute{
							Description: "Run event triggers.",
							Optional:    true,
							ElementType: types.StringType,
						},
						"email_addresses": schema.ListAttribute{
							Description: "Email addresses (for the email destination type).",
							Optional:    true,
							ElementType: types.StringType,
						},
						"enabled": schema.BoolAttribute{
							Description: "Whether the notification is enabled. Defaults to true.",
							Optional:    true,
						},
					},
				},
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

	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: c.BaseURL, Token: c.Token})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	r.tc = tc
}

func (r *autodiscoveryRuleResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan autodiscoveryRuleModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs := buildAutodiscoveryRuleAttrs(&plan)

	body, err := terrapod.MarshalResource("autodiscovery-rules", attrs, nil)
	if err != nil {
		resp.Diagnostics.AddError("Failed to marshal request", err.Error())
		return
	}

	data, err := r.tc.Post(ctx, "/api/terrapod/v1/autodiscovery-rules", body)
	if err != nil {
		resp.Diagnostics.AddError("Failed to create autodiscovery rule", err.Error())
		return
	}

	res, err := terrapod.ParseResource(data)
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

	data, err := r.tc.Get(ctx, "/api/terrapod/v1/autodiscovery-rules/"+state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Failed to read autodiscovery rule", err.Error())
		return
	}

	res, err := terrapod.ParseResource(data)
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

	body, err := terrapod.MarshalResourceWithID(state.ID.ValueString(), "autodiscovery-rules", attrs)
	if err != nil {
		resp.Diagnostics.AddError("Failed to marshal request", err.Error())
		return
	}

	data, err := r.tc.Patch(ctx, "/api/terrapod/v1/autodiscovery-rules/"+state.ID.ValueString(), body)
	if err != nil {
		resp.Diagnostics.AddError("Failed to update autodiscovery rule", err.Error())
		return
	}

	res, err := terrapod.ParseResource(data)
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

	err := r.tc.Delete(ctx, "/api/terrapod/v1/autodiscovery-rules/"+state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Failed to delete autodiscovery rule", err.Error())
		}
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

	// Optional (#314): omit entirely when unset so a rule that never set
	// it is left unchanged on the server (server defaults to "flag").
	if !m.OnDirectoryDelete.IsNull() && !m.OnDirectoryDelete.IsUnknown() {
		attrs["on-directory-delete"] = m.OnDirectoryDelete.ValueString()
	}

	// Optional templating fields (#318): omit entirely when unset so a
	// rule without them is left unchanged on the server.
	if !m.VarFiles.IsNull() && !m.VarFiles.IsUnknown() {
		varFiles := make([]string, 0, len(m.VarFiles.Elements()))
		for _, v := range m.VarFiles.Elements() {
			varFiles = append(varFiles, v.(types.String).ValueString())
		}
		attrs["var-files"] = varFiles
	}

	if !m.RunTaskTemplates.IsNull() && !m.RunTaskTemplates.IsUnknown() {
		tasks := make([]map[string]any, 0, len(m.RunTaskTemplates.Elements()))
		for _, e := range m.RunTaskTemplates.Elements() {
			obj := e.(types.Object)
			a := obj.Attributes()
			task := map[string]any{
				"name":  objStr(a, "name"),
				"url":   objStr(a, "url"),
				"stage": objStr(a, "stage"),
			}
			// Server validators read the hyphenated input keys.
			if s, ok := a["hmac_key"].(types.String); ok && !s.IsNull() && !s.IsUnknown() {
				task["hmac-key"] = s.ValueString()
			}
			if s, ok := a["enforcement_level"].(types.String); ok && !s.IsNull() && !s.IsUnknown() && s.ValueString() != "" {
				task["enforcement-level"] = s.ValueString()
			}
			if b, ok := a["enabled"].(types.Bool); ok && !b.IsNull() && !b.IsUnknown() {
				task["enabled"] = b.ValueBool()
			}
			tasks = append(tasks, task)
		}
		attrs["run-task-templates"] = tasks
	}

	if !m.NotificationTemplates.IsNull() && !m.NotificationTemplates.IsUnknown() {
		notifs := make([]map[string]any, 0, len(m.NotificationTemplates.Elements()))
		for _, e := range m.NotificationTemplates.Elements() {
			obj := e.(types.Object)
			a := obj.Attributes()
			notif := map[string]any{
				"name":             objStr(a, "name"),
				"destination-type": objStr(a, "destination_type"),
			}
			if s, ok := a["url"].(types.String); ok && !s.IsNull() && !s.IsUnknown() {
				notif["url"] = s.ValueString()
			}
			if s, ok := a["token"].(types.String); ok && !s.IsNull() && !s.IsUnknown() {
				notif["token"] = s.ValueString()
			}
			if l, ok := a["triggers"].(types.List); ok && !l.IsNull() && !l.IsUnknown() {
				notif["triggers"] = objStrList(l)
			}
			if l, ok := a["email_addresses"].(types.List); ok && !l.IsNull() && !l.IsUnknown() {
				notif["email-addresses"] = objStrList(l)
			}
			if b, ok := a["enabled"].(types.Bool); ok && !b.IsNull() && !b.IsUnknown() {
				notif["enabled"] = b.ValueBool()
			}
			notifs = append(notifs, notif)
		}
		attrs["notification-templates"] = notifs
	}

	return attrs
}

// objStr reads a required string field out of a nested object's attribute
// map, returning "" if absent/null.
func objStr(a map[string]attr.Value, key string) string {
	if s, ok := a[key].(types.String); ok && !s.IsNull() && !s.IsUnknown() {
		return s.ValueString()
	}
	return ""
}

// objStrList flattens a types.List of strings into a []string.
func objStrList(l types.List) []string {
	out := make([]string, 0, len(l.Elements()))
	for _, v := range l.Elements() {
		out = append(out, v.(types.String).ValueString())
	}
	return out
}

// readAutodiscoveryRuleIntoModel maps a JSON:API resource into the
// Terraform model.
func readAutodiscoveryRuleIntoModel(ctx context.Context, res *terrapod.Resource, m *autodiscoveryRuleModel) diag.Diagnostics {
	var diags diag.Diagnostics

	m.ID = types.StringValue(res.ID)

	m.Name = types.StringValue(terrapod.GetStringAttr(res, "name"))
	m.NameTemplate = types.StringValue(terrapod.GetStringAttr(res, "name-template"))
	m.VCSConnectionID = types.StringValue(terrapod.GetStringAttr(res, "vcs-connection-id"))
	m.RepoURL = types.StringValue(terrapod.GetStringAttr(res, "repo-url"))
	m.Branch = types.StringValue(terrapod.GetStringAttr(res, "branch"))
	m.Pattern = types.StringValue(terrapod.GetStringAttr(res, "pattern"))

	patterns := terrapod.GetListAttr(res, "ignore-patterns")
	if patterns == nil {
		patterns = []string{}
	}
	v, d := types.ListValueFrom(ctx, types.StringType, patterns)
	diags.Append(d...)
	m.IgnorePatterns = v

	m.Enabled = types.BoolValue(terrapod.GetBoolAttr(res, "enabled"))
	m.ExecutionMode = types.StringValue(terrapod.GetStringAttr(res, "execution-mode"))
	m.ExecutionBackend = types.StringValue(terrapod.GetStringAttr(res, "execution-backend"))
	m.AgentPoolID = types.StringValue(terrapod.GetStringAttr(res, "agent-pool-id"))
	m.TerraformVersion = types.StringValue(terrapod.GetStringAttr(res, "terraform-version"))
	m.ResourceCPU = types.StringValue(terrapod.GetStringAttr(res, "resource-cpu"))
	m.ResourceMemory = types.StringValue(terrapod.GetStringAttr(res, "resource-memory"))
	m.AutoApply = types.BoolValue(terrapod.GetBoolAttr(res, "auto-apply"))

	labels := terrapod.GetMapAttr(res, "labels")
	if labels == nil {
		labels = map[string]string{}
	}
	mv, dl := types.MapValueFrom(ctx, types.StringType, labels)
	diags.Append(dl...)
	m.Labels = mv

	m.OwnerEmail = types.StringValue(terrapod.GetStringAttr(res, "owner-email"))

	// Optional (#314). Tolerate missing/empty: an absent attribute leaves
	// the model field null so a rule that never set it produces no
	// spurious diff.
	if v := terrapod.GetStringAttr(res, "on-directory-delete"); v != "" {
		m.OnDirectoryDelete = types.StringValue(v)
	} else {
		m.OnDirectoryDelete = types.StringNull()
	}

	// Optional templating fields (#318). Tolerate missing/empty: an
	// absent attribute leaves the model field null so a rule that never
	// set them produces no spurious diff.
	if varFiles := terrapod.GetListAttr(res, "var-files"); len(varFiles) > 0 {
		v, d := types.ListValueFrom(ctx, types.StringType, varFiles)
		diags.Append(d...)
		m.VarFiles = v
	} else {
		m.VarFiles = types.ListNull(types.StringType)
	}

	rtObjType := types.ObjectType{AttrTypes: runTaskTemplateAttrTypes}
	if rawTasks := parseRawObjectList(res, "run-task-templates"); len(rawTasks) > 0 {
		elems := make([]attr.Value, 0, len(rawTasks))
		for _, t := range rawTasks {
			ov, d := types.ObjectValue(runTaskTemplateAttrTypes, map[string]attr.Value{
				"name":              rawStr(t, "name"),
				"url":               rawStr(t, "url"),
				"hmac_key":          rawStr(t, "hmac_key", "hmac-key"),
				"stage":             rawStr(t, "stage"),
				"enforcement_level": rawStr(t, "enforcement_level", "enforcement-level"),
				"enabled":           rawBool(t, "enabled"),
			})
			diags.Append(d...)
			elems = append(elems, ov)
		}
		lv, d := types.ListValue(rtObjType, elems)
		diags.Append(d...)
		m.RunTaskTemplates = lv
	} else {
		m.RunTaskTemplates = types.ListNull(rtObjType)
	}

	ntObjType := types.ObjectType{AttrTypes: notificationTemplateAttrTypes}
	if rawNotifs := parseRawObjectList(res, "notification-templates"); len(rawNotifs) > 0 {
		elems := make([]attr.Value, 0, len(rawNotifs))
		for _, n := range rawNotifs {
			trig, dt := rawStrList(ctx, n, "triggers")
			diags.Append(dt...)
			emails, de := rawStrList(ctx, n, "email_addresses", "email-addresses")
			diags.Append(de...)
			ov, d := types.ObjectValue(notificationTemplateAttrTypes, map[string]attr.Value{
				"name":             rawStr(n, "name"),
				"destination_type": rawStr(n, "destination_type", "destination-type"),
				"url":              rawStr(n, "url"),
				"token":            rawStr(n, "token"),
				"triggers":         trig,
				"email_addresses":  emails,
				"enabled":          rawBool(n, "enabled"),
			})
			diags.Append(d...)
			elems = append(elems, ov)
		}
		lv, d := types.ListValue(ntObjType, elems)
		diags.Append(d...)
		m.NotificationTemplates = lv
	} else {
		m.NotificationTemplates = types.ListNull(ntObjType)
	}

	m.CreatedAt = types.StringValue(terrapod.GetStringAttr(res, "created-at"))
	m.UpdatedAt = types.StringValue(terrapod.GetStringAttr(res, "updated-at"))

	return diags
}

// parseRawObjectList decodes a JSON:API attribute that is a list of
// objects into a slice of generic maps. Returns nil on missing/empty/
// non-list so callers can null the model field.
func parseRawObjectList(res *terrapod.Resource, key string) []map[string]any {
	raw, ok := res.Attributes[key]
	if !ok || len(raw) == 0 || string(raw) == "null" {
		return nil
	}
	var out []map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil
	}
	return out
}

// rawStr reads the first present string key from a decoded object,
// returning a null types.String if none of the keys are present or the
// value is not a string.
func rawStr(m map[string]any, keys ...string) types.String {
	for _, k := range keys {
		if v, ok := m[k]; ok && v != nil {
			if s, ok := v.(string); ok {
				return types.StringValue(s)
			}
		}
	}
	return types.StringNull()
}

// rawBool reads a bool key from a decoded object, returning a null
// types.Bool if absent or not a bool.
func rawBool(m map[string]any, key string) types.Bool {
	if v, ok := m[key]; ok && v != nil {
		if b, ok := v.(bool); ok {
			return types.BoolValue(b)
		}
	}
	return types.BoolNull()
}

// rawStrList reads the first present list-of-strings key from a decoded
// object into a types.List. Absent/empty yields a null list.
func rawStrList(ctx context.Context, m map[string]any, keys ...string) (types.List, diag.Diagnostics) {
	for _, k := range keys {
		v, ok := m[k]
		if !ok || v == nil {
			continue
		}
		arr, ok := v.([]any)
		if !ok || len(arr) == 0 {
			continue
		}
		strs := make([]string, 0, len(arr))
		for _, e := range arr {
			if s, ok := e.(string); ok {
				strs = append(strs, s)
			}
		}
		return types.ListValueFrom(ctx, types.StringType, strs)
	}
	return types.ListNull(types.StringType), nil
}
