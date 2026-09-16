// Package module_autodiscovery_rule implements the
// terrapod_module_autodiscovery_rule resource (#1584): a rule that finds
// modules in a repository — the root and any submodules — and registers them
// in the private registry. See docs/registry.md.
//
// API Contract (Terrapod API <-> Terraform Provider), via go-terrapod:
//
//	JSON:API type: "module-autodiscovery-rules"
//	ID format: "modrule-<uuid>"
//	Create:  POST   /api/terrapod/v1/module-autodiscovery-rules
//	Read:    GET    /api/terrapod/v1/module-autodiscovery-rules/{id}
//	Update:  PATCH  /api/terrapod/v1/module-autodiscovery-rules/{id}
//	Delete:  DELETE /api/terrapod/v1/module-autodiscovery-rules/{id}
//
// Attribute mapping (JSON:API attribute -> Terraform schema attribute):
//
//	"name"              -> name              (string, required)
//	"vcs-connection-id" -> vcs_connection_id (string, required)
//	"repo-url"          -> repo_url          (string, required)
//	"pattern"           -> pattern           (string, required)
//	"branch"            -> branch            (string, optional, "" = default branch)
//	"ignore-patterns"   -> ignore_patterns   (list of strings, optional)
//	"enabled"           -> enabled           (bool, optional, default true)
//	"name-template"     -> name_template     (string, optional)
//	"provider"          -> module_provider   (string, optional, "" = from the repository name;
//	                                         "provider" is a reserved root attribute name)
//	"vcs-tag-pattern"   -> vcs_tag_pattern   (string, optional, default "v*")
//	"labels"            -> labels            (map[string]string, optional)
//	"owner-email"       -> owner_email       (string, optional)
//
// Read-only:
//
//	"target-kind"        -> target_kind        (string, computed: repository | namespace | pattern)
//	"first-scan-at"      -> first_scan_at      (string, computed)
//	"last-scanned-sha"   -> last_scanned_sha   (string, computed)
//	"last-enumerated-at" -> last_enumerated_at (string, computed)
//	"last-error"         -> last_error         (string, computed)
//	"created-at"         -> created_at         (string, computed)
//	"updated-at"         -> updated_at         (string, computed)
//
// repo_url names one repository, an org or group, or a pattern over one
// namespace's repositories (#1620); the server classifies it on save. The
// plan-time check here only catches what can never be valid: a glob outside
// the last path segment.
//
// Creating the rule registers nothing: the registry poller records what is in
// the repository and registers only directories that appear afterwards. Modules
// already there are registered with the API's scan (UI, MCP or go-terrapod),
// or declared as terrapod_registry_module resources.
//
// Import: by rule ID ("modrule-<uuid>").
package module_autodiscovery_rule

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/listplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/mapplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/schema/validator"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var (
	_ resource.Resource                = &moduleRuleResource{}
	_ resource.ResourceWithImportState = &moduleRuleResource{}
)

type moduleRuleModel struct {
	ID               types.String `tfsdk:"id"`
	Name             types.String `tfsdk:"name"`
	VCSConnectionID  types.String `tfsdk:"vcs_connection_id"`
	RepoURL          types.String `tfsdk:"repo_url"`
	Branch           types.String `tfsdk:"branch"`
	Pattern          types.String `tfsdk:"pattern"`
	IgnorePatterns   types.List   `tfsdk:"ignore_patterns"`
	Enabled          types.Bool   `tfsdk:"enabled"`
	NameTemplate     types.String `tfsdk:"name_template"`
	Provider         types.String `tfsdk:"module_provider"`
	VCSTagPattern    types.String `tfsdk:"vcs_tag_pattern"`
	Labels           types.Map    `tfsdk:"labels"`
	OwnerEmail       types.String `tfsdk:"owner_email"`
	TargetKind       types.String `tfsdk:"target_kind"`
	FirstScanAt      types.String `tfsdk:"first_scan_at"`
	LastScannedSHA   types.String `tfsdk:"last_scanned_sha"`
	LastEnumeratedAt types.String `tfsdk:"last_enumerated_at"`
	LastError        types.String `tfsdk:"last_error"`
	CreatedAt        types.String `tfsdk:"created_at"`
	UpdatedAt        types.String `tfsdk:"updated_at"`
}

type moduleRuleResource struct {
	tc *terrapod.Client
}

// NewResource returns the terrapod_module_autodiscovery_rule resource.
func NewResource() resource.Resource {
	return &moduleRuleResource{}
}

func (r *moduleRuleResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_module_autodiscovery_rule"
}

func (r *moduleRuleResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a Terrapod module autodiscovery rule: it finds the modules in a repository, or in every " +
			"repository of an org, group or name pattern — the root and any submodules, each a directory of Terraform " +
			"files matching `pattern` — and registers them in the private registry. Creating the rule registers " +
			"nothing; directories that appear on the tracked branch afterwards, and repositories created after the " +
			"rule, are registered automatically. " +
			"See https://github.com/mattrobinsonsre/terrapod/blob/main/docs/registry.md.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description:   "The rule ID (modrule-<uuid>).",
				Computed:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"name": schema.StringAttribute{
				Description: "Display name for the rule. Unique per VCS connection.",
				Required:    true,
			},
			"vcs_connection_id": schema.StringAttribute{
				Description: "The VCS connection whose credentials read the repository (e.g. \"vcs-abc123\").",
				Required:    true,
			},
			"repo_url": schema.StringAttribute{
				Description: "What to scan, in one of three forms: one repository " +
					"(https://github.com/myorg/terraform-aws-network); an org or group (https://github.com/myorg, " +
					"https://gitlab.com/mygroup/platform), covering every repository in it; or a pattern over one " +
					"namespace's repositories (https://github.com/myorg/terraform-*), with glob characters in the last " +
					"segment only. The server decides which when the rule is saved, reports it as target_kind, and " +
					"never changes it on its own. Forks are skipped.",
				Required:   true,
				Validators: []validator.String{repoURLValidator{}},
			},
			"pattern": schema.StringAttribute{
				Description: "Glob matched against the repository's .tf/.tf.json file paths (gitignore-style with ** support). " +
					"Each matching file's directory is a module. Examples, tests, fixtures and hidden directories never are.",
				Required: true,
			},
			"branch": schema.StringAttribute{
				Description: "Branch to scan. Empty (default) uses the repository's default branch.",
				Optional:    true,
				Computed:    true,
				Default:     stringdefault.StaticString(""),
			},
			"ignore_patterns": schema.ListAttribute{
				Description:   "Globs whose matching files never count towards a module.",
				Optional:      true,
				Computed:      true,
				ElementType:   types.StringType,
				PlanModifiers: []planmodifier.List{listplanmodifier.UseStateForUnknown()},
			},
			"enabled": schema.BoolAttribute{
				Description: "Whether the registry poller registers new directories automatically. Defaults to true.",
				Optional:    true,
				Computed:    true,
				Default:     booldefault.StaticBool(true),
			},
			"name_template": schema.StringAttribute{
				Description: "Template for the registered modules' names, using {repo}, {path}, {leaf}, {root} and " +
					"{owner} (the repository's owner or group). Empty (default) is the repository's module name, then " +
					"the submodule's directory.",
				Optional: true,
				Computed: true,
				Default:  stringdefault.StaticString(""),
			},
			"module_provider": schema.StringAttribute{
				Description: "Provider for the registered modules (e.g. aws). Empty (default) takes it from a " +
					"terraform-<provider>-<name> repository name.",
				Optional: true,
				Computed: true,
				Default:  stringdefault.StaticString(""),
			},
			"vcs_tag_pattern": schema.StringAttribute{
				Description: "Tag pattern the registered modules publish versions from. Defaults to \"v*\".",
				Optional:    true,
				Computed:    true,
				Default:     stringdefault.StaticString("v*"),
			},
			"labels": schema.MapAttribute{
				Description:   "Labels given to the registered modules — feeds registry RBAC and filtering.",
				Optional:      true,
				Computed:      true,
				ElementType:   types.StringType,
				PlanModifiers: []planmodifier.Map{mapplanmodifier.UseStateForUnknown()},
			},
			"owner_email": schema.StringAttribute{
				Description: "Owner of the registered modules. Empty = no owner; label RBAC alone decides access.",
				Optional:    true,
				Computed:    true,
				Default:     stringdefault.StaticString(""),
			},
			// Server-maintained scan bookkeeping: changes out of band (the poller)
			// and resets when the rule moves to another repository, so no
			// UseStateForUnknown — a planned value could then be wrong.
			"target_kind": schema.StringAttribute{
				Description: "What repo_url names, as the server classified it: \"repository\", \"namespace\" (an org " +
					"or group) or \"pattern\". Changes only when repo_url or vcs_connection_id does.",
				Computed: true,
			},
			"first_scan_at": schema.StringAttribute{
				Description: "When the rule first read its repository (RFC3339); empty if it has not yet.",
				Computed:    true,
			},
			"last_scanned_sha": schema.StringAttribute{
				Description: "The tracked branch's head commit at the rule's last scan. Empty for an org-wide rule, " +
					"whose heads are per repository.",
				Computed: true,
			},
			"last_enumerated_at": schema.StringAttribute{
				Description: "When an org-wide rule last listed its repositories (RFC3339); empty if it has not yet, " +
					"and always for a single-repository rule.",
				Computed: true,
			},
			"last_error": schema.StringAttribute{
				Description: "Why the rule's last poll failed — for example its target was deleted, or could not be " +
					"listed — or empty. A rule in error registers nothing until repo_url is saved again.",
				Computed: true,
			},
			"created_at": schema.StringAttribute{
				Description:   "Creation timestamp.",
				Computed:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"updated_at": schema.StringAttribute{
				Description: "Last update timestamp.",
				Computed:    true,
			},
		},
	}
}

func (r *moduleRuleResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	c, ok := req.ProviderData.(*client.Client)
	if !ok {
		resp.Diagnostics.AddError("Unexpected provider data type", fmt.Sprintf("Expected *client.Client, got %T", req.ProviderData))
		return
	}
	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: c.BaseURL, Token: c.Token})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	r.tc = tc
}

func (r *moduleRuleResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan moduleRuleModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	rule, err := r.tc.CreateModuleAutodiscoveryRule(ctx, requestFromModel(ctx, &plan))
	if err != nil {
		resp.Diagnostics.AddError("Failed to create module autodiscovery rule", err.Error())
		return
	}
	resp.Diagnostics.Append(readIntoModel(ctx, rule, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *moduleRuleResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state moduleRuleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	rule, err := r.tc.GetModuleAutodiscoveryRule(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Failed to read module autodiscovery rule", err.Error())
		return
	}
	resp.Diagnostics.Append(readIntoModel(ctx, rule, &state)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *moduleRuleResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state moduleRuleModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	rule, err := r.tc.UpdateModuleAutodiscoveryRule(ctx, state.ID.ValueString(), requestFromModel(ctx, &plan))
	if err != nil {
		resp.Diagnostics.AddError("Failed to update module autodiscovery rule", err.Error())
		return
	}
	resp.Diagnostics.Append(readIntoModel(ctx, rule, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *moduleRuleResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state moduleRuleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.tc.DeleteModuleAutodiscoveryRule(ctx, state.ID.ValueString()); err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Failed to delete module autodiscovery rule", err.Error())
		}
	}
}

func (r *moduleRuleResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

// requestFromModel sends every configurable field, so an update converges the
// rule on the configuration. Unknown lists and maps (optional + computed, not
// configured) are left out and keep the server's value.
func requestFromModel(ctx context.Context, m *moduleRuleModel) terrapod.ModuleAutodiscoveryRuleRequest {
	str := func(v types.String) *string {
		if v.IsNull() || v.IsUnknown() {
			return nil
		}
		s := v.ValueString()
		return &s
	}
	req := terrapod.ModuleAutodiscoveryRuleRequest{
		Name:            str(m.Name),
		VCSConnectionID: str(m.VCSConnectionID),
		RepoURL:         str(m.RepoURL),
		Branch:          str(m.Branch),
		Pattern:         str(m.Pattern),
		NameTemplate:    str(m.NameTemplate),
		Provider:        str(m.Provider),
		VCSTagPattern:   str(m.VCSTagPattern),
		OwnerEmail:      str(m.OwnerEmail),
	}
	if !m.Enabled.IsNull() && !m.Enabled.IsUnknown() {
		b := m.Enabled.ValueBool()
		req.Enabled = &b
	}
	if !m.IgnorePatterns.IsNull() && !m.IgnorePatterns.IsUnknown() {
		ip := []string{}
		_ = m.IgnorePatterns.ElementsAs(ctx, &ip, false)
		req.IgnorePatterns = &ip
	}
	if !m.Labels.IsNull() && !m.Labels.IsUnknown() {
		l := map[string]string{}
		_ = m.Labels.ElementsAs(ctx, &l, false)
		req.Labels = &l
	}
	return req
}

// sameConnection reports whether two VCS connection ids name the same
// connection, with or without the "vcs-" prefix.
func sameConnection(a, b string) bool {
	return strings.TrimPrefix(a, "vcs-") == strings.TrimPrefix(b, "vcs-")
}

// normalizeTagPattern mirrors the server: surrounding whitespace is dropped and
// an empty tag pattern means "v*".
func normalizeTagPattern(s string) string {
	if t := strings.TrimSpace(s); t != "" {
		return t
	}
	return "v*"
}

// normalizeIgnorePatterns mirrors the server: entries are trimmed and blank
// ones dropped.
func normalizeIgnorePatterns(ps []string) []string {
	out := []string{}
	for _, p := range ps {
		if t := strings.TrimSpace(p); t != "" {
			out = append(out, t)
		}
	}
	return out
}

// keepForm returns the value already in the model (the plan on Create/Update,
// the prior state on Read) when it normalises to what the server returned,
// otherwise the server's value. The server tidies several fields — trimming
// whitespace, defaulting an empty tag pattern to "v*" — and writing its tidied
// value over a configured one fails the apply as "Provider produced
// inconsistent result after apply" (#1634).
func keepForm(current types.String, server string, norm func(string) string) types.String {
	if !current.IsNull() && !current.IsUnknown() && norm(current.ValueString()) == norm(server) {
		return current
	}
	return types.StringValue(server)
}

func sameStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// readIntoModel copies the server's rule into the model. Each field keeps the
// form it was configured in when the server's value is that form normalised:
// a bare UUID for the prefixed VCS connection id, padded strings the server
// trims, an empty tag pattern it stores as "v*", blank ignore patterns it drops.
func readIntoModel(ctx context.Context, rule *terrapod.ModuleAutodiscoveryRule, m *moduleRuleModel) diag.Diagnostics {
	var diags diag.Diagnostics
	m.ID = types.StringValue(rule.ID)
	m.Name = keepForm(m.Name, rule.Name, strings.TrimSpace)
	if m.VCSConnectionID.IsNull() || m.VCSConnectionID.IsUnknown() ||
		!sameConnection(m.VCSConnectionID.ValueString(), rule.VCSConnectionID) {
		m.VCSConnectionID = types.StringValue(rule.VCSConnectionID)
	}
	m.RepoURL = keepForm(m.RepoURL, rule.RepoURL, strings.TrimSpace)
	m.Branch = keepForm(m.Branch, rule.Branch, strings.TrimSpace)
	m.Pattern = keepForm(m.Pattern, rule.Pattern, strings.TrimSpace)

	ignore := rule.IgnorePatterns
	if ignore == nil {
		ignore = []string{}
	}
	keepIgnore := false
	if !m.IgnorePatterns.IsNull() && !m.IgnorePatterns.IsUnknown() {
		configured := []string{}
		diags.Append(m.IgnorePatterns.ElementsAs(ctx, &configured, false)...)
		keepIgnore = sameStrings(normalizeIgnorePatterns(configured), normalizeIgnorePatterns(ignore))
	}
	if !keepIgnore {
		lv, d := types.ListValueFrom(ctx, types.StringType, ignore)
		diags.Append(d...)
		m.IgnorePatterns = lv
	}

	m.Enabled = types.BoolValue(rule.Enabled)
	m.NameTemplate = types.StringValue(rule.NameTemplate)
	m.Provider = keepForm(m.Provider, rule.Provider, strings.TrimSpace)
	m.VCSTagPattern = keepForm(m.VCSTagPattern, rule.VCSTagPattern, normalizeTagPattern)

	labels := rule.Labels
	if labels == nil {
		labels = map[string]string{}
	}
	mv, d := types.MapValueFrom(ctx, types.StringType, labels)
	diags.Append(d...)
	m.Labels = mv

	m.OwnerEmail = keepForm(m.OwnerEmail, rule.OwnerEmail, strings.TrimSpace)
	m.TargetKind = types.StringValue(rule.TargetKind)
	m.FirstScanAt = types.StringValue(rule.FirstScanAt)
	m.LastScannedSHA = types.StringValue(rule.LastScannedSHA)
	m.LastEnumeratedAt = types.StringValue(rule.LastEnumeratedAt)
	m.LastError = types.StringValue(rule.LastError)
	m.CreatedAt = types.StringValue(rule.CreatedAt)
	m.UpdatedAt = types.StringValue(rule.UpdatedAt)
	return diags
}

// repoURLValidator catches, at plan time, the one repo_url shape no provider
// accepts: glob characters (*, ? or [...]) outside the last path segment. It
// does not decide repository vs org vs pattern — that needs the provider's API,
// and the server does it on apply.
type repoURLValidator struct{}

func (repoURLValidator) Description(context.Context) string {
	return "glob characters (*, ?, [ ]) may appear only in the last path segment"
}

func (v repoURLValidator) MarkdownDescription(ctx context.Context) string { return v.Description(ctx) }

func (repoURLValidator) ValidateString(_ context.Context, req validator.StringRequest, resp *validator.StringResponse) {
	if req.ConfigValue.IsNull() || req.ConfigValue.IsUnknown() {
		return
	}
	if msg := repoURLProblem(req.ConfigValue.ValueString()); msg != "" {
		resp.Diagnostics.AddAttributeError(req.Path, "Invalid repo_url", msg)
	}
}

// repoURLProblem returns why value can never be a valid repo_url, or "".
func repoURLProblem(value string) string {
	s := strings.TrimSpace(value)
	if s == "" {
		return "repo_url must not be empty"
	}
	// Drop the scheme and host (https://host/…) or the scp-style prefix
	// (git@host:…), leaving the path.
	if i := strings.Index(s, "://"); i >= 0 {
		s = s[i+3:]
		if j := strings.Index(s, "/"); j >= 0 {
			s = s[j+1:]
		} else {
			s = ""
		}
	} else if at := strings.Index(s, "@"); at >= 0 {
		if colon := strings.Index(s[at:], ":"); colon >= 0 {
			s = s[at+colon+1:]
		}
	}
	s = strings.TrimSuffix(strings.Trim(s, "/"), ".git")
	segments := strings.Split(s, "/")
	for _, seg := range segments[:len(segments)-1] {
		if strings.ContainsAny(seg, "*?[]") {
			return fmt.Sprintf("glob characters (*, ?, [ ]) may appear only in the last path segment of %q; "+
				"a pattern matches repositories directly inside one org or group", value)
		}
	}
	return ""
}
