package workspace

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
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/listplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/setplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var (
	_ resource.Resource                   = &workspaceResource{}
	_ resource.ResourceWithImportState    = &workspaceResource{}
	_ resource.ResourceWithValidateConfig = &workspaceResource{}
)

// workspaceResource holds two clients during the provider's migration to
// go-terrapod (#347). Workspace CRUD uses the new typed methods on `tc`;
// the remote-state-consumers helpers below still use `client` because
// they live outside the workspaces resource and migrate in a later pass
// alongside the standalone terrapod_remote_state_consumer resource. Once
// both have migrated, the `client` field disappears.
type workspaceResource struct {
	client *client.Client
	tc     *terrapod.Client
}

// NewResource returns a new workspace resource.
func NewResource() resource.Resource {
	return &workspaceResource{}
}

func (r *workspaceResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_workspace"
}

// ValidateConfig rejects a config that sets both agent-pool attributes.
//
// The server returns 422 for the same combination (#1085); catching it at plan
// time turns an apply-time failure into an error the operator sees before
// anything is sent.
func (r *workspaceResource) ValidateConfig(ctx context.Context, req resource.ValidateConfigRequest, resp *resource.ValidateConfigResponse) {
	var m workspaceModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if !m.AgentPoolID.IsNull() && !m.AgentPoolIDs.IsNull() {
		resp.Diagnostics.AddError(
			"Conflicting agent pool attributes",
			"Set either agent_pool_id or agent_pool_ids, not both. agent_pool_id assigns a "+
				"single pool (replacing any set); agent_pool_ids assigns the whole set.",
		)
	}
}

// unmanagedCollections are the Optional+Computed collection attributes whose
// plan value falls back to prior state when the config omits them
// (`UseStateForUnknown`). That fallback is deliberate — the server may hold a
// value this config does not manage, set through the UI or the bulk-update
// endpoint, and clobbering it on every apply would be worse. The cost is that
// *removing* the attribute from a config is indistinguishable from never having
// declared it, so it plans as no-change and the server-side value survives
// (#1091). ModifyPlan below makes that visible rather than silent.
var unmanagedCollections = []struct {
	name  string
	value func(*workspaceModel) attr.Value
}{
	{"agent_pool_ids", func(m *workspaceModel) attr.Value { return m.AgentPoolIDs }},
	{"var_files", func(m *workspaceModel) attr.Value { return m.VarFiles }},
	{"trigger_prefixes", func(m *workspaceModel) attr.Value { return m.TriggerPrefixes }},
	{"drift_ignore_rules", func(m *workspaceModel) attr.Value { return m.DriftIgnoreRules }},
	{"security_scan_skip_rules", func(m *workspaceModel) attr.Value { return m.SecurityScanSkipRules }},
}

// agentPoolIDsForRequest returns the pool set to put on the wire, or nil when it
// must be omitted (#1094).
//
// The server rejects a request carrying BOTH `agent-pool-id` and
// `agent-pool-ids` (422), and the singular already means "this one pool,
// replacing any existing set" — so when the singular is set, it travels alone.
//
// The guard cannot live in ValidateConfig. `agent_pool_ids` is Optional+Computed
// with UseStateForUnknown, so a config that omits it still gets a KNOWN,
// non-null planned value: the prior state. Updating a workspace that had a pool
// set therefore populated both fields and 422'd every time, while create (no
// prior state, so the plan value is unknown) worked. In the *configuration* only
// the singular is set — the collision is manufactured from state at
// request-build time, which is the only place it can be caught.
func agentPoolIDsForRequest(poolID types.String, poolIDs types.List) []string {
	if !poolID.IsNull() || poolIDs.IsNull() || poolIDs.IsUnknown() {
		return nil
	}
	out := make([]string, 0, len(poolIDs.Elements()))
	for _, v := range poolIDs.Elements() {
		out = append(out, v.(types.String).ValueString())
	}
	return out
}

// onlyPool reports whether a pool-set list holds exactly the one given pool —
// the case where a singular `agent_pool_id` config genuinely manages the set.
func onlyPool(v attr.Value, id string) bool {
	l, ok := v.(types.List)
	if !ok || l.IsNull() || l.IsUnknown() || len(l.Elements()) != 1 {
		return false
	}
	s, ok := l.Elements()[0].(types.String)
	return ok && s.ValueString() == id
}

// hasElements reports whether a list attribute holds at least one element.
func hasElements(v attr.Value) bool {
	l, ok := v.(types.List)
	if !ok || l.IsNull() || l.IsUnknown() {
		return false
	}
	return len(l.Elements()) > 0
}

// ModifyPlan warns when the workspace holds a value for an Optional+Computed
// collection that the configuration does not declare (#1091).
//
// Terraform would otherwise report "no changes" while the config and the remote
// object genuinely disagree — the shape that bites is deleting a `var_files`
// line to retire a tfvars file, reading the empty plan as confirmation, and
// deleting the file while the workspace still passes `-var-file` for it.
//
// This is a warning rather than a real diff on purpose. The framework cannot
// tell "removed from config" from "never in config": both arrive here as a null
// config value over a state value the read-back populated from the server.
// Planning a clear would therefore also clear values legitimately managed
// out-of-band — and, worse, would do it on the first plan after upgrading the
// provider, silently. A warning costs nothing and cannot destroy anything.
func (r *workspaceResource) ModifyPlan(ctx context.Context, req resource.ModifyPlanRequest, resp *resource.ModifyPlanResponse) {
	// Nothing to compare on create (no prior state) or destroy (no plan).
	if req.State.Raw.IsNull() || req.Plan.Raw.IsNull() {
		return
	}
	var cfg, state workspaceModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &cfg)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	for _, a := range unmanagedCollections {
		// `agent_pool_ids` reads back the set that `agent_pool_id` assigns, so a
		// config using the singular IS managing it — as long as the set is
		// exactly the one pool it names. When the workspace holds MORE pools
		// than that, the config does not manage the extras and the apply will
		// drop them (the singular replaces the whole set), so that case must
		// still warn. Exempting it unconditionally suppressed the warning in
		// precisely the situation it exists for (#1094).
		if a.name == "agent_pool_ids" && !cfg.AgentPoolID.IsNull() {
			if onlyPool(state.AgentPoolIDs, cfg.AgentPoolID.ValueString()) {
				continue
			}
			// The apply is about to replace the set, so the planned value must
			// NOT be the prior state that UseStateForUnknown supplied — the
			// framework would compare [A,B] against the applied [A] and fail
			// with "Provider produced inconsistent result after apply: element 1
			// has vanished". Mark it unknown so the read-back is authoritative.
			resp.Diagnostics.Append(
				resp.Plan.SetAttribute(ctx, path.Root("agent_pool_ids"),
					types.ListUnknown(types.StringType))...,
			)
			resp.Diagnostics.AddAttributeWarning(
				path.Root("agent_pool_id"),
				"Applying agent_pool_id will drop the workspace's other agent pools",
				"The workspace runs on several agent pools, but this configuration sets the "+
					"singular `agent_pool_id`, which replaces the whole set with that one "+
					"pool. Terraform reports no change to `agent_pool_ids` because the "+
					"configuration does not declare it.\n\n"+
					"To keep the set, use `agent_pool_ids = [...]` instead. Losing pools "+
					"reduces this workspace to a single point of execution failure.",
			)
			continue
		}
		if !a.value(&cfg).IsNull() || !hasElements(a.value(&state)) {
			continue
		}
		resp.Diagnostics.AddAttributeWarning(
			path.Root(a.name),
			"Workspace has "+a.name+" that this configuration does not manage",
			"The workspace has a non-empty `"+a.name+"` but the configuration does not "+
				"declare it, so Terraform will report no changes and the existing value "+
				"will be left in place.\n\n"+
				"If you removed `"+a.name+"` from the configuration intending to clear it, "+
				"set `"+a.name+" = []` instead — omitting the attribute means \"leave "+
				"alone\", not \"clear\".\n\n"+
				"If the value is managed outside Terraform (the UI or the bulk-update "+
				"endpoint), this warning is expected; declare the attribute explicitly to "+
				"silence it.",
		)
	}
}

func (r *workspaceResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a Terrapod workspace.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description: "The workspace ID (e.g. ws-abc123).",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"name": schema.StringAttribute{
				Description: "The workspace name.",
				Required:    true,
			},
			"execution_mode": schema.StringAttribute{
				Description: "Execution mode: local or agent.",
				Optional:    true,
				Computed:    true,
				Default:     stringdefault.StaticString("local"),
			},
			"auto_apply": schema.BoolAttribute{
				Description: "Automatically apply successful plans. Superseded by `auto_apply_mode` when that is set; prefer `auto_apply_mode` for new configurations.",
				Optional:    true,
				Computed:    true,
				Default:     booldefault.StaticBool(false),
			},
			"auto_apply_mode": schema.StringAttribute{
				Description: "Conditional auto-apply: `never`, `always`, `create` (apply only plans that add resources) or `create_update` (also allow in-place updates). `create` and `create_update` never apply a plan that destroys or replaces a resource — that is left for a human. Optional + Computed with no default, so a configuration that does not set it inherits whatever the workspace has and never drifts.",
				Optional:    true,
				Computed:    true,
			},
			"execution_backend": schema.StringAttribute{
				Description: "Execution backend: terraform or tofu.",
				Optional:    true,
				Computed:    true,
				Default:     stringdefault.StaticString("terraform"),
			},
			"terraform_version": schema.StringAttribute{
				Description: "The Terraform/OpenTofu version to use.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"terragrunt_enabled": schema.BoolAttribute{
				Description: "Wrap tofu/terraform with terragrunt for agent-mode runs. See https://terrapod.dev for the agent-mode limitations.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.Bool{
					boolplanmodifier.UseStateForUnknown(),
				},
			},
			"terragrunt_version": schema.StringAttribute{
				Description: "The terragrunt CLI version to use when terragrunt_enabled is true. Partial versions (e.g. \"1.0\") are resolved by the binary cache. Defaults to \"1.0\".",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"working_directory": schema.StringAttribute{
				Description: "Working directory relative to the repo root.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"resource_cpu": schema.StringAttribute{
				Description: "CPU request for runner Jobs (K8s format, e.g. '1').",
				Optional:    true,
				Computed:    true,
				Default:     stringdefault.StaticString("1"),
			},
			"resource_memory": schema.StringAttribute{
				Description: "Memory request for runner Jobs (K8s format, e.g. '2Gi').",
				Optional:    true,
				Computed:    true,
				Default:     stringdefault.StaticString("2Gi"),
			},
			"labels": schema.MapAttribute{
				Description: "Labels for RBAC evaluation (key-value pairs).",
				Optional:    true,
				ElementType: types.StringType,
			},
			"vcs_repo_url": schema.StringAttribute{
				Description: "Git HTTPS URL for VCS integration.",
				Optional:    true,
			},
			"vcs_branch": schema.StringAttribute{
				Description: "Branch to track (empty = repo default).",
				Optional:    true,
			},
			"vcs_connection_id": schema.StringAttribute{
				Description: "VCS connection ID (e.g. vcs-abc123).",
				Optional:    true,
			},
			"vcs_workflow": schema.StringAttribute{
				Description: "VCS workflow mode: `merge_then_apply` (default, TFE/HCP standard) or `apply_then_merge` (Atlantis-style; PR runs are full plan-and-apply that wait on a `terrapod apply` comment). Apply-then-merge requires a VCS connection and is incompatible with `auto_apply=true`.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"auto_merge": schema.BoolAttribute{
				Description: "If true, Terrapod merges the PR/MR after a successful apply (subject to branch protection). Default: false.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.Bool{
					boolplanmodifier.UseStateForUnknown(),
				},
			},
			"auto_merge_strategy": schema.StringAttribute{
				Description: "Merge strategy for auto-merge: `merge` (default), `squash`, or `rebase`.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"agent_pool_id": schema.StringAttribute{
				Description: "Agent pool ID for agent execution mode. Assigns exactly one pool, replacing any existing set — use `agent_pool_ids` to assign several. Reads back as element 0 of the set. Conflicts with `agent_pool_ids`.",
				// Optional + Computed, same #684 rationale as agent_pool_ids: the
				// server ALWAYS answers this attribute with element 0 of the pool
				// set (tfe_v2.py, "a projection, NOT a preference"). So a config
				// that assigns pools via the plural and omits this one plans null
				// and applies "apool-<first>" — "inconsistent result after apply",
				// which aborts the create outright. Computed lets the server own it.
				Optional: true,
				Computed: true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"agent_pool_ids": schema.ListAttribute{
				Description: "Agent pools this workspace's runs may execute on (#1085). The set is flat: a queued run is offered to every pool at once and whichever pool has a live listener claims it first, so losing one pool does not stop the workspace. There is no primary and no ordering preference. `agent_pool_id` reads back as element 0. Conflicts with `agent_pool_id`. Omitting this attribute leaves any existing server-side value untouched — it does not clear it; set `agent_pool_ids = []` to clear.",
				// Optional + Computed with UseStateForUnknown — same rationale as
				// drift_ignore_rules (#684): the server may hold a set this config
				// doesn't manage (assigned via the UI or bulk-update), so omitting
				// it means "leave alone", not "clear".
				Optional:    true,
				Computed:    true,
				ElementType: types.StringType,
				PlanModifiers: []planmodifier.List{
					listplanmodifier.UseStateForUnknown(),
				},
			},
			"var_files": schema.ListAttribute{
				Description: "List of .tfvars file paths passed as -var-file arguments to plan/apply. Omitting this attribute leaves any existing server-side value untouched — it does not clear it; set `var_files = []` to clear.",
				// Optional + Computed with UseStateForUnknown — same rationale as
				// drift_ignore_rules (#684): tolerate a server-held value the
				// config doesn't set. Set `= []` to clear.
				Optional:    true,
				Computed:    true,
				ElementType: types.StringType,
				PlanModifiers: []planmodifier.List{
					listplanmodifier.UseStateForUnknown(),
				},
			},
			"trigger_prefixes": schema.ListAttribute{
				Description: "Repo-root-relative directories to include in the sparse VCS fetch in addition to `working_directory`. Required when the workspace's terraform crosses directory boundaries via relative module sources (`module \"foo\" { source = \"../foo\" }`) — sparse-checkout cone mode includes parents of the listed directories but NOT siblings, so the referenced sibling must be declared here or the runner will error with `Unable to evaluate directory symlink`. Omitting this attribute leaves any existing server-side value untouched — it does not clear it; set `trigger_prefixes = []` to clear.",
				Optional:    true,
				Computed:    true,
				ElementType: types.StringType,
				PlanModifiers: []planmodifier.List{
					listplanmodifier.UseStateForUnknown(),
				},
			},
			"drift_ignore_rules": schema.ListAttribute{
				Description: "Glob-aware patterns suppressed by the drift-result classifier (#482). Each entry is a Terraform resource address optionally suffixed with a dotted attribute path; `*` matches zero or more non-`.` characters (so it can span `[N]` indices but not segment boundaries), and `[*]` matches any bracketed index. A bare address with no attribute suffix silences any change to that resource — including destroys — so use carefully. Examples: `aws_iam_role.foo.tags.Environment`, `aws_autoscaling_group.workers[*].desired_capacity`, `module.eks*.argocd_cluster.*.config.tls_client_config.ca_data`, `aws_iam_role.foo`. Empty list (the default) means classic drift behaviour: every plan diff counts. Affects drift-detection runs only — regular plan/apply is untouched. Omitting this attribute leaves any existing server-side value untouched — it does not clear it; set `drift_ignore_rules = []` to clear.",
				// Optional + Computed: the server may hold a value this config
				// doesn't set (e.g. set out-of-band via the bulk-update endpoint),
				// so omitting it must mean "leave alone" (plan = unknown), not
				// "force null" — otherwise the read-back's non-null value fails
				// the framework's "inconsistent result after apply" check (#684).
				// Set `= []` to explicitly clear.
				Optional:    true,
				Computed:    true,
				ElementType: types.StringType,
				PlanModifiers: []planmodifier.List{
					listplanmodifier.UseStateForUnknown(),
				},
			},
			"drift_detection_enabled": schema.BoolAttribute{
				Description: "Enable drift detection for this workspace. Defaults to true for VCS-connected workspaces, false otherwise.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.Bool{
					boolplanmodifier.UseStateForUnknown(),
				},
			},
			"drift_detection_interval_seconds": schema.Int64Attribute{
				Description: "Interval in seconds between drift detection checks.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.Int64{
					int64planmodifier.UseStateForUnknown(),
				},
			},
			// Security-scan (Checkov/Trivy IaC misconfig scanning, #1036). All four
			// are Optional+Computed with UseStateForUnknown for the same reason as
			// drift_ignore_rules (#684): the server holds defaults the config need
			// not set, so omitting one means "leave alone", not "force null".
			"security_scan_enforcement": schema.StringAttribute{
				Description: "IaC security-scan enforcement: `off` (skip), `advisory` (scan, never block — the default), or `enforced` (a failed/errored scan blocks apply until fixed or overridden).",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"security_scan_engine": schema.StringAttribute{
				Description: "Which scanner(s) run: `checkov` (default), `trivy`, or `both` (union of findings, deduped).",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"security_scan_severity_threshold": schema.StringAttribute{
				Description: "Lowest finding severity that counts as a failure: `critical`, `high` (default), `medium`, or `low`.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"security_scan_skip_rules": schema.ListAttribute{
				Description: "Scanner rule-ids to suppress (Checkov `CKV_*` / Trivy `AVD-*`). Empty list (default) skips nothing. Omitting this attribute leaves any existing server-side value untouched — it does not clear it; set `security_scan_skip_rules = []` to clear.",
				Optional:    true,
				Computed:    true,
				ElementType: types.StringType,
				PlanModifiers: []planmodifier.List{
					listplanmodifier.UseStateForUnknown(),
				},
			},
			"plan_expiry_seconds": schema.Int64Attribute{
				Description: "Per-workspace plan expiry TTL in seconds (#646). An apply-capable planned run older than this is auto-discarded and must be re-planned. Unset / 0 = disabled (the default).",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.Int64{
					int64planmodifier.UseStateForUnknown(),
				},
			},
			"ai_summary_mode": schema.StringAttribute{
				Description: "Per-workspace AI plan-summary opt-in (#401). One of \"default\" (follow the deployment's global `ai_summary.enabled` setting), \"enabled\" (always summarise this workspace's plans), or \"disabled\" (never summarise — overrides global). Defaults to \"default\".",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"ai_summary_context": schema.StringAttribute{
				Description: "Workspace-specific facts appended to the AI summariser's prompt (#401). Additive to the deployment-wide fleet context. Use to flag blast-radius concerns or domain knowledge the model should weigh when describing changes for this workspace. Max 4000 characters.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"slack_channel": schema.StringAttribute{
				Description: "Slack channel this workspace's run notifications post to (#556) — approval / applied / errored / drift. Opt-in: leave empty and the workspace stays silent (there is no deployment-wide fan-out). Requires the Slack app to be enabled server-side.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"remote_state_consumers": schema.SetAttribute{
				Description: "Workspace IDs authorized to read this workspace's state via `terraform_remote_state` (#344). Optional + Computed: leave null to opt out of managing the set here (server side is left intact — useful when consumers are managed via standalone `terrapod_remote_state_consumer` resources elsewhere). Set to `[]` to explicitly remove all consumers. **Do not mix this attribute with standalone `terrapod_remote_state_consumer` resources targeting the same producer** — the two will drift on every plan and fight each other. Mutations require admin/write on this (producer) workspace; a consumer team cannot self-grant.",
				Optional:    true,
				Computed:    true,
				ElementType: types.StringType,
				PlanModifiers: []planmodifier.Set{
					setplanmodifier.UseStateForUnknown(),
				},
			},

			// Read-only
			"owner_email": schema.StringAttribute{
				Description: "Email of the workspace owner.",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			// `UseStateForUnknown` ONLY belongs on Computed-only attrs
			// whose value definitely does NOT change as a side effect of
			// the apply. v0.35.5 applied it everywhere and broke apply
			// on every workspace because the server-volatile timestamps
			// (updated_at ticks on every PATCH; vcs_last_polled_at ticks
			// on every poll cycle) produced plan-vs-apply-time mismatches
			// → terraform-plugin-framework's consistency check aborted.
			//
			// Safe-for-UseStateForUnknown set (each field's invariant):
			//   created_at         — immutable after creation
			//   owner_email        — only the platform admin can change; PATCH never does
			//   agent_pool_name    — only changes when agent_pool_id changes (caller's PATCH)
			//   vcs_connection_name — only changes when vcs_connection_id changes
			//   state_diverged     — only set/cleared by a state upload pathway; not by PATCH
			//
			// Server-volatile set (NO UseStateForUnknown — plan will
			// honestly show `(known after apply)`; the diff noise is the
			// right answer because the value can legitimately change
			// between plan and apply):
			//   updated_at, vcs_last_polled_at, vcs_last_error,
			//   vcs_last_error_at, drift_status, drift_last_checked_at,
			//   drift_latest_run_id, lifecycle_state, lifecycle_reason,
			//   locked
			"drift_status": schema.StringAttribute{
				Description: "Current drift status: \"\" (never checked), \"no_drift\", \"drifted\", or \"errored\". Server-volatile — updates when a drift run completes.",
				Computed:    true,
			},
			"drift_last_checked_at": schema.StringAttribute{
				Description: "Timestamp of the last drift check. Server-volatile.",
				Computed:    true,
			},
			"drift_latest_run_id": schema.StringAttribute{
				Description: "ID of the drift run that produced the current `drift_status`, prefixed `run-…`. Empty when drift has never run or when cleared by a successful apply. Server-volatile.",
				Computed:    true,
			},
			"state_diverged": schema.BoolAttribute{
				Description: "True when an apply Job succeeded but uploading the resulting state to Terrapod failed; the recorded state is out of sync with reality. Stable across PATCHes — only the state-upload pathway changes this.",
				Computed:    true,
				PlanModifiers: []planmodifier.Bool{
					boolplanmodifier.UseStateForUnknown(),
				},
			},
			"lifecycle_state": schema.StringAttribute{
				Description: "Autodiscovery lifecycle state for managed workspaces: \"active\", \"pending_deletion\", or \"archived\". Server-volatile — autodiscovery lifecycle reconciler can move this between plan and apply.",
				Computed:    true,
			},
			"lifecycle_reason": schema.StringAttribute{
				Description: "Human-readable explanation of `lifecycle_state`. Empty for active workspaces. Server-volatile.",
				Computed:    true,
			},
			"vcs_last_polled_at": schema.StringAttribute{
				Description: "Timestamp of the most recent successful VCS poll cycle. Server-volatile — VCS poller writes this every `vcs.poll_interval_seconds` (default 60s).",
				Computed:    true,
			},
			"vcs_last_attempted_at": schema.StringAttribute{
				Description: "Timestamp of the most recent VCS poll attempt, successful or not. `vcs_last_polled_at` only advances on success, so a gap between the two identifies a workspace whose polls are failing. Server-volatile.",
				Computed:    true,
			},
			"vcs_last_error": schema.StringAttribute{
				Description: "Most recent VCS poll error message. Empty when the last poll succeeded. Server-volatile.",
				Computed:    true,
			},
			"vcs_last_error_at": schema.StringAttribute{
				Description: "Timestamp of `vcs_last_error`. Server-volatile.",
				Computed:    true,
			},
			"agent_pool_name": schema.StringAttribute{
				Description: "Human-readable name of the assigned agent pool, server-derived from `agent_pool_id`. Only changes when `agent_pool_id` changes.",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"vcs_connection_name": schema.StringAttribute{
				Description: "Human-readable name of the assigned VCS connection, server-derived from `vcs_connection_id`. Only changes when `vcs_connection_id` changes.",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"locked": schema.BoolAttribute{
				Description: "Whether the workspace is locked. Server-volatile — operators can lock/unlock via the API outside of Terraform.",
				Computed:    true,
			},
			"created_at": schema.StringAttribute{
				Description: "Creation timestamp.",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"updated_at": schema.StringAttribute{
				Description: "Last update timestamp. Server-volatile — ticks on every PATCH and on every server-side write (drift detection, VCS poll, lifecycle reconciler).",
				Computed:    true,
			},
		},
	}
}

func (r *workspaceResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	c, ok := req.ProviderData.(*client.Client)
	if !ok {
		resp.Diagnostics.AddError("Unexpected provider data type", fmt.Sprintf("Expected *client.Client, got %T", req.ProviderData))
		return
	}
	r.client = c

	// Build the go-terrapod client from the same BaseURL+Token. Both
	// clients share the operator's auth + endpoint configuration; only
	// the call shapes differ. SkipTLSVerify is captured indirectly via
	// the shared HTTPClient when the provider was configured with it
	// — we re-derive here so go-terrapod's defaults (TLS 1.3 minimum)
	// apply consistently with the rest of the SDK consumers.
	tc, err := terrapod.NewClient(terrapod.Options{
		BaseURL: c.BaseURL,
		Token:   c.Token,
	})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	r.tc = tc
}

func (r *workspaceResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan workspaceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	createReq, dgs := buildCreateWorkspaceRequest(ctx, &plan)
	resp.Diagnostics.Append(dgs...)
	if resp.Diagnostics.HasError() {
		return
	}

	ws, err := r.tc.CreateWorkspace(ctx, createReq)
	if err != nil {
		resp.Diagnostics.AddError("Failed to create workspace", err.Error())
		return
	}

	resp.Diagnostics.Append(readWorkspaceIntoModel(ctx, ws, &plan)...)

	// Apply the consumer set from plan (if managed here), then refresh
	// the attribute from the server (#344, #348). Null in plan ⇒
	// unmanaged ⇒ no PUT but we still read for state consistency.
	if err := applyConsumersFromPlan(ctx, r.tc, plan.ID.ValueString(), plan.RemoteStateConsumers); err != nil {
		resp.Diagnostics.AddError("Failed to apply remote_state_consumers", err.Error())
		return
	}
	consumers, dgs := readRemoteStateConsumers(ctx, r.tc, plan.ID.ValueString())
	resp.Diagnostics.Append(dgs...)
	plan.RemoteStateConsumers = consumers

	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *workspaceResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state workspaceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	ws, err := r.tc.GetWorkspace(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Failed to read workspace", err.Error())
		return
	}

	resp.Diagnostics.Append(readWorkspaceIntoModel(ctx, ws, &state)...)

	// Refresh the consumer set from server (#344, #348). Always read,
	// even when the user manages this via standalone resources — the
	// Optional+Computed schema means a null config value falls back
	// to the state value during plan, so no spurious diff.
	consumers, dgs := readRemoteStateConsumers(ctx, r.tc, state.ID.ValueString())
	resp.Diagnostics.Append(dgs...)
	state.RemoteStateConsumers = consumers

	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *workspaceResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan workspaceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var state workspaceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	updateReq, dgs := buildUpdateWorkspaceRequest(ctx, &plan)
	resp.Diagnostics.Append(dgs...)
	if resp.Diagnostics.HasError() {
		return
	}

	ws, err := r.tc.UpdateWorkspace(ctx, state.ID.ValueString(), updateReq)
	if err != nil {
		resp.Diagnostics.AddError("Failed to update workspace", err.Error())
		return
	}

	resp.Diagnostics.Append(readWorkspaceIntoModel(ctx, ws, &plan)...)

	// Apply the consumer set from plan if managed here, then refresh.
	// Same null = unmanaged convention as Create (#344, #348).
	if err := applyConsumersFromPlan(ctx, r.tc, plan.ID.ValueString(), plan.RemoteStateConsumers); err != nil {
		resp.Diagnostics.AddError("Failed to apply remote_state_consumers", err.Error())
		return
	}
	consumers, dgs := readRemoteStateConsumers(ctx, r.tc, plan.ID.ValueString())
	resp.Diagnostics.Append(dgs...)
	plan.RemoteStateConsumers = consumers

	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *workspaceResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state workspaceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	// go-terrapod handles the Terrapod-native path (/api/terrapod/v1/
	// rather than /api/v2/, which returns 405 — see #353). The 404-on-
	// idempotent-delete is unwrapped to match the original behaviour.
	err := r.tc.DeleteWorkspace(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Failed to delete workspace", err.Error())
		}
	}
}

func (r *workspaceResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	// Import by workspace name — resolve to ID via go-terrapod's
	// GetWorkspaceByName. Terraform then calls Read with the resolved
	// id to populate the rest of the state.
	ws, err := r.tc.GetWorkspaceByName(ctx, req.ID)
	if err != nil {
		resp.Diagnostics.AddError("Failed to import workspace", fmt.Sprintf("Could not find workspace %q: %s", req.ID, err))
		return
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), ws.ID)...)
}

// buildCreateWorkspaceRequest translates a Terraform plan into the
// go-terrapod CreateWorkspaceRequest. Optional fields the operator
// didn't set (IsNull / IsUnknown) stay zero-valued in the struct so
// the SDK omits them from the JSON:API attributes — matching the
// previous map-based behaviour where only set keys were sent.
func buildCreateWorkspaceRequest(ctx context.Context, m *workspaceModel) (terrapod.CreateWorkspaceRequest, diag.Diagnostics) {
	var diags diag.Diagnostics
	req := terrapod.CreateWorkspaceRequest{Name: m.Name.ValueString()}

	if !m.ExecutionMode.IsNull() && !m.ExecutionMode.IsUnknown() {
		req.ExecutionMode = m.ExecutionMode.ValueString()
	}
	// `auto_apply` has a schema Default, so it is always known here — sending
	// it alongside `auto_apply_mode` would hit the API's either/or 422. The
	// mode is the richer setting, so it wins and the boolean is left off.
	if !m.AutoApplyMode.IsNull() && !m.AutoApplyMode.IsUnknown() && m.AutoApplyMode.ValueString() != "" {
		v := m.AutoApplyMode.ValueString()
		req.AutoApplyMode = &v
	} else if !m.AutoApply.IsNull() && !m.AutoApply.IsUnknown() {
		v := m.AutoApply.ValueBool()
		req.AutoApply = &v
	}
	if !m.ExecutionBackend.IsNull() && !m.ExecutionBackend.IsUnknown() {
		req.ExecutionBackend = m.ExecutionBackend.ValueString()
	}
	if !m.TerraformVersion.IsNull() && !m.TerraformVersion.IsUnknown() {
		req.TerraformVersion = m.TerraformVersion.ValueString()
	}
	if !m.TerragruntEnabled.IsNull() && !m.TerragruntEnabled.IsUnknown() {
		v := m.TerragruntEnabled.ValueBool()
		req.TerragruntEnabled = &v
	}
	if !m.TerragruntVersion.IsNull() && !m.TerragruntVersion.IsUnknown() {
		req.TerragruntVersion = m.TerragruntVersion.ValueString()
	}
	if !m.WorkingDirectory.IsNull() && !m.WorkingDirectory.IsUnknown() {
		req.WorkingDirectory = m.WorkingDirectory.ValueString()
	}
	if !m.ResourceCPU.IsNull() && !m.ResourceCPU.IsUnknown() {
		req.ResourceCPU = m.ResourceCPU.ValueString()
	}
	if !m.ResourceMemory.IsNull() && !m.ResourceMemory.IsUnknown() {
		req.ResourceMemory = m.ResourceMemory.ValueString()
	}
	if !m.VCSRepoURL.IsNull() {
		req.VCSRepoURL = m.VCSRepoURL.ValueString()
	}
	if !m.VCSBranch.IsNull() {
		req.VCSBranch = m.VCSBranch.ValueString()
	}
	if !m.VCSWorkflow.IsNull() && !m.VCSWorkflow.IsUnknown() {
		req.VCSWorkflow = m.VCSWorkflow.ValueString()
	}
	if !m.AutoMerge.IsNull() && !m.AutoMerge.IsUnknown() {
		v := m.AutoMerge.ValueBool()
		req.AutoMerge = &v
	}
	if !m.AutoMergeStrategy.IsNull() && !m.AutoMergeStrategy.IsUnknown() {
		req.AutoMergeStrategy = m.AutoMergeStrategy.ValueString()
	}
	if !m.VCSConnectionID.IsNull() && !m.VCSConnectionID.IsUnknown() {
		req.VCSConnectionID = m.VCSConnectionID.ValueString()
	}
	if !m.AgentPoolID.IsNull() {
		req.AgentPoolID = m.AgentPoolID.ValueString()
	}
	req.AgentPoolIDs = agentPoolIDsForRequest(m.AgentPoolID, m.AgentPoolIDs)
	if !m.Labels.IsNull() && !m.Labels.IsUnknown() {
		labels := map[string]string{}
		for k, v := range m.Labels.Elements() {
			labels[k] = v.(types.String).ValueString()
		}
		req.Labels = labels
	}
	if !m.VarFiles.IsNull() && !m.VarFiles.IsUnknown() {
		varFiles := []string{}
		for _, v := range m.VarFiles.Elements() {
			varFiles = append(varFiles, v.(types.String).ValueString())
		}
		req.VarFiles = varFiles
	}
	if !m.TriggerPrefixes.IsNull() && !m.TriggerPrefixes.IsUnknown() {
		triggerPrefixes := []string{}
		for _, v := range m.TriggerPrefixes.Elements() {
			triggerPrefixes = append(triggerPrefixes, v.(types.String).ValueString())
		}
		req.TriggerPrefixes = triggerPrefixes
	}
	if !m.DriftIgnoreRules.IsNull() && !m.DriftIgnoreRules.IsUnknown() {
		rules := []string{}
		for _, v := range m.DriftIgnoreRules.Elements() {
			rules = append(rules, v.(types.String).ValueString())
		}
		req.DriftIgnoreRules = rules
	}
	if !m.DriftDetectionEnabled.IsNull() && !m.DriftDetectionEnabled.IsUnknown() {
		v := m.DriftDetectionEnabled.ValueBool()
		req.DriftDetectionEnabled = &v
	}
	if !m.DriftDetectionIntervalSeconds.IsNull() && !m.DriftDetectionIntervalSeconds.IsUnknown() {
		v := m.DriftDetectionIntervalSeconds.ValueInt64()
		req.DriftDetectionIntervalSeconds = &v
	}
	if !m.SecurityScanEnforcement.IsNull() && !m.SecurityScanEnforcement.IsUnknown() {
		req.SecurityScanEnforcement = m.SecurityScanEnforcement.ValueString()
	}
	if !m.SecurityScanEngine.IsNull() && !m.SecurityScanEngine.IsUnknown() {
		req.SecurityScanEngine = m.SecurityScanEngine.ValueString()
	}
	if !m.SecurityScanSeverityThreshold.IsNull() && !m.SecurityScanSeverityThreshold.IsUnknown() {
		req.SecurityScanSeverityThreshold = m.SecurityScanSeverityThreshold.ValueString()
	}
	if !m.SecurityScanSkipRules.IsNull() && !m.SecurityScanSkipRules.IsUnknown() {
		rules := []string{}
		for _, v := range m.SecurityScanSkipRules.Elements() {
			rules = append(rules, v.(types.String).ValueString())
		}
		req.SecurityScanSkipRules = rules
	}
	if !m.PlanExpirySeconds.IsNull() && !m.PlanExpirySeconds.IsUnknown() {
		v := m.PlanExpirySeconds.ValueInt64()
		req.PlanExpirySeconds = &v
	}
	if !m.AISummaryMode.IsNull() && !m.AISummaryMode.IsUnknown() {
		req.AISummaryMode = m.AISummaryMode.ValueString()
	}
	if !m.AISummaryContext.IsNull() && !m.AISummaryContext.IsUnknown() {
		req.AISummaryContext = m.AISummaryContext.ValueString()
	}
	if !m.SlackChannel.IsNull() && !m.SlackChannel.IsUnknown() {
		req.SlackChannel = m.SlackChannel.ValueString()
	}
	return req, diags
}

// buildUpdateWorkspaceRequest is the partial-update counterpart to
// buildCreateWorkspaceRequest. Same translation logic; Name is
// included so a Terraform-driven rename round-trips via PATCH (the
// API supports rename — terrapod-vcs-test moves between names
// during e.g. the cutover smoke).
func buildUpdateWorkspaceRequest(ctx context.Context, m *workspaceModel) (terrapod.UpdateWorkspaceRequest, diag.Diagnostics) {
	var diags diag.Diagnostics
	req := terrapod.UpdateWorkspaceRequest{}

	if !m.Name.IsNull() && !m.Name.IsUnknown() {
		req.Name = m.Name.ValueString()
	}
	if !m.ExecutionMode.IsNull() && !m.ExecutionMode.IsUnknown() {
		req.ExecutionMode = m.ExecutionMode.ValueString()
	}
	// `auto_apply` has a schema Default, so it is always known here — sending
	// it alongside `auto_apply_mode` would hit the API's either/or 422. The
	// mode is the richer setting, so it wins and the boolean is left off.
	if !m.AutoApplyMode.IsNull() && !m.AutoApplyMode.IsUnknown() && m.AutoApplyMode.ValueString() != "" {
		v := m.AutoApplyMode.ValueString()
		req.AutoApplyMode = &v
	} else if !m.AutoApply.IsNull() && !m.AutoApply.IsUnknown() {
		v := m.AutoApply.ValueBool()
		req.AutoApply = &v
	}
	if !m.ExecutionBackend.IsNull() && !m.ExecutionBackend.IsUnknown() {
		req.ExecutionBackend = m.ExecutionBackend.ValueString()
	}
	if !m.TerraformVersion.IsNull() && !m.TerraformVersion.IsUnknown() {
		req.TerraformVersion = m.TerraformVersion.ValueString()
	}
	if !m.TerragruntEnabled.IsNull() && !m.TerragruntEnabled.IsUnknown() {
		v := m.TerragruntEnabled.ValueBool()
		req.TerragruntEnabled = &v
	}
	if !m.TerragruntVersion.IsNull() && !m.TerragruntVersion.IsUnknown() {
		req.TerragruntVersion = m.TerragruntVersion.ValueString()
	}
	if !m.WorkingDirectory.IsNull() && !m.WorkingDirectory.IsUnknown() {
		req.WorkingDirectory = m.WorkingDirectory.ValueString()
	}
	if !m.ResourceCPU.IsNull() && !m.ResourceCPU.IsUnknown() {
		req.ResourceCPU = m.ResourceCPU.ValueString()
	}
	if !m.ResourceMemory.IsNull() && !m.ResourceMemory.IsUnknown() {
		req.ResourceMemory = m.ResourceMemory.ValueString()
	}
	if !m.VCSRepoURL.IsNull() {
		req.VCSRepoURL = m.VCSRepoURL.ValueString()
	}
	if !m.VCSBranch.IsNull() {
		req.VCSBranch = m.VCSBranch.ValueString()
	}
	if !m.VCSWorkflow.IsNull() && !m.VCSWorkflow.IsUnknown() {
		req.VCSWorkflow = m.VCSWorkflow.ValueString()
	}
	if !m.AutoMerge.IsNull() && !m.AutoMerge.IsUnknown() {
		v := m.AutoMerge.ValueBool()
		req.AutoMerge = &v
	}
	if !m.AutoMergeStrategy.IsNull() && !m.AutoMergeStrategy.IsUnknown() {
		req.AutoMergeStrategy = m.AutoMergeStrategy.ValueString()
	}
	if !m.VCSConnectionID.IsNull() && !m.VCSConnectionID.IsUnknown() {
		req.VCSConnectionID = m.VCSConnectionID.ValueString()
	}
	if !m.AgentPoolID.IsNull() {
		req.AgentPoolID = m.AgentPoolID.ValueString()
	}
	req.AgentPoolIDs = agentPoolIDsForRequest(m.AgentPoolID, m.AgentPoolIDs)
	if !m.Labels.IsNull() && !m.Labels.IsUnknown() {
		labels := map[string]string{}
		for k, v := range m.Labels.Elements() {
			labels[k] = v.(types.String).ValueString()
		}
		req.Labels = labels
	}
	if !m.VarFiles.IsNull() && !m.VarFiles.IsUnknown() {
		varFiles := []string{}
		for _, v := range m.VarFiles.Elements() {
			varFiles = append(varFiles, v.(types.String).ValueString())
		}
		req.VarFiles = varFiles
	}
	if !m.TriggerPrefixes.IsNull() && !m.TriggerPrefixes.IsUnknown() {
		triggerPrefixes := []string{}
		for _, v := range m.TriggerPrefixes.Elements() {
			triggerPrefixes = append(triggerPrefixes, v.(types.String).ValueString())
		}
		req.TriggerPrefixes = triggerPrefixes
	}
	if !m.DriftIgnoreRules.IsNull() && !m.DriftIgnoreRules.IsUnknown() {
		rules := []string{}
		for _, v := range m.DriftIgnoreRules.Elements() {
			rules = append(rules, v.(types.String).ValueString())
		}
		req.DriftIgnoreRules = rules
	}
	if !m.DriftDetectionEnabled.IsNull() && !m.DriftDetectionEnabled.IsUnknown() {
		v := m.DriftDetectionEnabled.ValueBool()
		req.DriftDetectionEnabled = &v
	}
	if !m.DriftDetectionIntervalSeconds.IsNull() && !m.DriftDetectionIntervalSeconds.IsUnknown() {
		v := m.DriftDetectionIntervalSeconds.ValueInt64()
		req.DriftDetectionIntervalSeconds = &v
	}
	if !m.SecurityScanEnforcement.IsNull() && !m.SecurityScanEnforcement.IsUnknown() {
		req.SecurityScanEnforcement = m.SecurityScanEnforcement.ValueString()
	}
	if !m.SecurityScanEngine.IsNull() && !m.SecurityScanEngine.IsUnknown() {
		req.SecurityScanEngine = m.SecurityScanEngine.ValueString()
	}
	if !m.SecurityScanSeverityThreshold.IsNull() && !m.SecurityScanSeverityThreshold.IsUnknown() {
		req.SecurityScanSeverityThreshold = m.SecurityScanSeverityThreshold.ValueString()
	}
	if !m.SecurityScanSkipRules.IsNull() && !m.SecurityScanSkipRules.IsUnknown() {
		rules := []string{}
		for _, v := range m.SecurityScanSkipRules.Elements() {
			rules = append(rules, v.(types.String).ValueString())
		}
		req.SecurityScanSkipRules = rules
	}
	if !m.PlanExpirySeconds.IsNull() && !m.PlanExpirySeconds.IsUnknown() {
		v := m.PlanExpirySeconds.ValueInt64()
		req.PlanExpirySeconds = &v
	}
	if !m.AISummaryMode.IsNull() && !m.AISummaryMode.IsUnknown() {
		req.AISummaryMode = m.AISummaryMode.ValueString()
	}
	if !m.AISummaryContext.IsNull() && !m.AISummaryContext.IsUnknown() {
		v := m.AISummaryContext.ValueString()
		req.AISummaryContext = &v
	}
	if !m.SlackChannel.IsNull() && !m.SlackChannel.IsUnknown() {
		v := m.SlackChannel.ValueString()
		req.SlackChannel = &v
	}
	return req, diags
}

// readWorkspaceIntoModel populates the Terraform model from a typed
// go-terrapod Workspace. Replaces the previous Resource/Attribute-map
// based readResourceIntoModel — the SDK does the JSON:API parsing now.
func readWorkspaceIntoModel(ctx context.Context, ws *terrapod.Workspace, m *workspaceModel) diag.Diagnostics {
	var diags diag.Diagnostics

	m.ID = types.StringValue(ws.ID)
	m.Name = types.StringValue(ws.Name)
	m.ExecutionMode = types.StringValue(ws.ExecutionMode)
	m.AutoApply = types.BoolValue(ws.AutoApply)
	m.AutoApplyMode = types.StringValue(ws.AutoApplyMode)
	m.ExecutionBackend = types.StringValue(ws.ExecutionBackend)
	m.WorkingDirectory = types.StringValue(ws.WorkingDirectory)
	m.ResourceCPU = types.StringValue(ws.ResourceCPU)
	m.ResourceMemory = types.StringValue(ws.ResourceMemory)
	m.VCSWorkflow = types.StringValue(ws.VCSWorkflow)
	m.AutoMerge = types.BoolValue(ws.AutoMerge)
	m.AutoMergeStrategy = types.StringValue(ws.AutoMergeStrategy)
	m.OwnerEmail = types.StringValue(ws.OwnerEmail)
	m.Locked = types.BoolValue(ws.Locked)
	m.CreatedAt = types.StringValue(ws.CreatedAt)
	m.UpdatedAt = types.StringValue(ws.UpdatedAt)

	// Nullable string fields — empty string from the SDK means absent
	// on the server; Terraform null preserves "computed-default" UX.
	if ws.TerraformVersion != "" {
		m.TerraformVersion = types.StringValue(ws.TerraformVersion)
	} else {
		m.TerraformVersion = types.StringNull()
	}
	m.TerragruntEnabled = types.BoolValue(ws.TerragruntEnabled)
	if ws.TerragruntVersion != "" {
		m.TerragruntVersion = types.StringValue(ws.TerragruntVersion)
	} else {
		m.TerragruntVersion = types.StringNull()
	}
	if ws.VCSRepoURL != "" {
		m.VCSRepoURL = types.StringValue(ws.VCSRepoURL)
	} else {
		m.VCSRepoURL = types.StringNull()
	}
	if ws.VCSBranch != "" {
		m.VCSBranch = types.StringValue(ws.VCSBranch)
	} else {
		m.VCSBranch = types.StringNull()
	}
	if ws.AgentPoolID != "" {
		m.AgentPoolID = types.StringValue(ws.AgentPoolID)
	} else {
		m.AgentPoolID = types.StringNull()
	}
	// Agent pool set (#1085) — same null-vs-empty rule as var_files above: a
	// config that never declared the attribute must not flip to `[]`, and a
	// config that declared `[]` must not flip to null.
	if m.AgentPoolIDs.IsNull() && len(ws.AgentPoolIDs) == 0 {
		m.AgentPoolIDs = types.ListNull(types.StringType)
	} else {
		apVal, apDiag := types.ListValueFrom(ctx, types.StringType, ws.AgentPoolIDs)
		diags.Append(apDiag...)
		m.AgentPoolIDs = apVal
	}
	if ws.VCSConnectionID != "" {
		m.VCSConnectionID = types.StringValue(ws.VCSConnectionID)
	} else {
		m.VCSConnectionID = types.StringNull()
	}

	// Drift detection
	m.DriftDetectionEnabled = types.BoolValue(ws.DriftDetectionEnabled)
	if ws.DriftDetectionIntervalSeconds != nil && *ws.DriftDetectionIntervalSeconds > 0 {
		m.DriftDetectionIntervalSeconds = types.Int64Value(*ws.DriftDetectionIntervalSeconds)
	} else {
		m.DriftDetectionIntervalSeconds = types.Int64Null()
	}

	// Security-scan (#1036) — server-provided, always defaulted.
	m.SecurityScanEnforcement = types.StringValue(ws.SecurityScanEnforcement)
	m.SecurityScanEngine = types.StringValue(ws.SecurityScanEngine)
	m.SecurityScanSeverityThreshold = types.StringValue(ws.SecurityScanSeverityThreshold)
	if ws.PlanExpirySeconds != nil && *ws.PlanExpirySeconds > 0 {
		m.PlanExpirySeconds = types.Int64Value(*ws.PlanExpirySeconds)
	} else {
		m.PlanExpirySeconds = types.Int64Null()
	}
	if ws.DriftStatus != "" {
		m.DriftStatus = types.StringValue(ws.DriftStatus)
	} else {
		m.DriftStatus = types.StringNull()
	}
	if ws.DriftLastCheckedAt != "" {
		m.DriftLastCheckedAt = types.StringValue(ws.DriftLastCheckedAt)
	} else {
		m.DriftLastCheckedAt = types.StringNull()
	}
	if ws.DriftLatestRunID != "" {
		m.DriftLatestRunID = types.StringValue(ws.DriftLatestRunID)
	} else {
		m.DriftLatestRunID = types.StringNull()
	}

	// State + lifecycle + VCS poll status — read-only fields the server
	// surfaces for diagnostics and operator UX. Empty-string from the
	// SDK becomes Terraform null so a fresh workspace doesn't show
	// "empty string" values in the state diff.
	m.StateDiverged = types.BoolValue(ws.StateDiverged)
	if ws.LifecycleState != "" {
		m.LifecycleState = types.StringValue(ws.LifecycleState)
	} else {
		m.LifecycleState = types.StringNull()
	}
	if ws.LifecycleReason != "" {
		m.LifecycleReason = types.StringValue(ws.LifecycleReason)
	} else {
		m.LifecycleReason = types.StringNull()
	}
	if ws.VCSLastPolledAt != "" {
		m.VCSLastPolledAt = types.StringValue(ws.VCSLastPolledAt)
	} else {
		m.VCSLastPolledAt = types.StringNull()
	}
	if ws.VCSLastAttemptedAt != "" {
		m.VCSLastAttemptedAt = types.StringValue(ws.VCSLastAttemptedAt)
	} else {
		m.VCSLastAttemptedAt = types.StringNull()
	}
	if ws.VCSLastError != "" {
		m.VCSLastError = types.StringValue(ws.VCSLastError)
	} else {
		m.VCSLastError = types.StringNull()
	}
	if ws.VCSLastErrorAt != "" {
		m.VCSLastErrorAt = types.StringValue(ws.VCSLastErrorAt)
	} else {
		m.VCSLastErrorAt = types.StringNull()
	}
	if ws.AgentPoolName != "" {
		m.AgentPoolName = types.StringValue(ws.AgentPoolName)
	} else {
		m.AgentPoolName = types.StringNull()
	}
	if ws.VCSConnectionName != "" {
		m.VCSConnectionName = types.StringValue(ws.VCSConnectionName)
	} else {
		m.VCSConnectionName = types.StringNull()
	}

	// AI plan summary (#401). The server always returns a concrete
	// value for `ai-summary-mode` (defaulting to "default"); the
	// context is the empty string for new workspaces. Pin both to
	// concrete StringValues so Terraform doesn't see "unknown" drift.
	if ws.AISummaryMode != "" {
		m.AISummaryMode = types.StringValue(ws.AISummaryMode)
	} else {
		m.AISummaryMode = types.StringValue("default")
	}
	m.AISummaryContext = types.StringValue(ws.AISummaryContext)
	m.SlackChannel = types.StringValue(ws.SlackChannel)

	// Var files — same null-vs-empty rule as trigger_prefixes above.
	if m.VarFiles.IsNull() && len(ws.VarFiles) == 0 {
		m.VarFiles = types.ListNull(types.StringType)
	} else {
		vfVal, vfDiag := types.ListValueFrom(ctx, types.StringType, ws.VarFiles)
		diags.Append(vfDiag...)
		m.VarFiles = vfVal
	}

	// Trigger prefixes — repo paths beyond `working_directory` that must
	// land in the sparse-checkout fetch.
	//
	// The null-vs-empty-list ambiguity is the most-bitten edge in the
	// terraform-plugin-framework Optional list pattern, and we've been
	// burned by both directions:
	//
	// - v0.35.4: Read coerced `[]` to null. A caller that declared
	//   `trigger_prefixes = []` got plan=[] / apply=null mismatch.
	// - v0.35.5: Read coerced null to `[]`. A caller that OMITTED the
	//   field got plan=null / apply=[] mismatch on the very first
	//   apply against the new provider.
	//
	// Right answer: respect what the prior state holds. The framework
	// passes the prior state into this function via `m`, so checking
	// `m.TriggerPrefixes.IsNull()` BEFORE we overwrite it lets us
	// preserve null when the caller's config + prior state were both
	// null and the API just returned its default empty list. Only
	// materialise as `[]` when the prior state already had a non-null
	// list (or when the API returned a populated list).
	if m.TriggerPrefixes.IsNull() && len(ws.TriggerPrefixes) == 0 {
		// prior null + server empty → preserve null (caller omitted it)
		m.TriggerPrefixes = types.ListNull(types.StringType)
	} else {
		tpVal, tpDiag := types.ListValueFrom(ctx, types.StringType, ws.TriggerPrefixes)
		diags.Append(tpDiag...)
		m.TriggerPrefixes = tpVal
	}

	// Drift-ignore rules (#482) — same null-vs-empty preservation rule
	// as trigger_prefixes. Server default `[]` is preserved as null in
	// state when the caller didn't declare the attribute.
	if m.DriftIgnoreRules.IsNull() && len(ws.DriftIgnoreRules) == 0 {
		m.DriftIgnoreRules = types.ListNull(types.StringType)
	} else {
		dirVal, dirDiag := types.ListValueFrom(ctx, types.StringType, ws.DriftIgnoreRules)
		diags.Append(dirDiag...)
		m.DriftIgnoreRules = dirVal
	}

	// Security-scan skip rules (#1036) — same null-vs-empty rule as drift_ignore_rules.
	if m.SecurityScanSkipRules.IsNull() && len(ws.SecurityScanSkipRules) == 0 {
		m.SecurityScanSkipRules = types.ListNull(types.StringType)
	} else {
		ssVal, ssDiag := types.ListValueFrom(ctx, types.StringType, ws.SecurityScanSkipRules)
		diags.Append(ssDiag...)
		m.SecurityScanSkipRules = ssVal
	}

	// Labels — same null-vs-empty-map rule as trigger_prefixes above.
	// `len(nil-map) == 0` so an empty server map collapses to the
	// preserve-null branch when prior state was null.
	if m.Labels.IsNull() && len(ws.Labels) == 0 {
		m.Labels = types.MapNull(types.StringType)
	} else {
		val, d := types.MapValueFrom(ctx, types.StringType, ws.Labels)
		diags.Append(d...)
		m.Labels = val
	}
	return diags
}

// putRemoteStateConsumers declaratively replaces the producer's full
// consumer set via the #344 PUT endpoint. Empty `ids` means "remove
// all". Server-side enforces admin on the producer.
func putRemoteStateConsumers(ctx context.Context, c *terrapod.Client, workspaceID string, ids []string) error {
	items := make([]map[string]any, len(ids))
	for i, id := range ids {
		items[i] = map[string]any{"type": "workspaces", "id": id}
	}
	body, err := json.Marshal(map[string]any{"data": items})
	if err != nil {
		return err
	}
	_, err = c.Put(ctx, fmt.Sprintf("/api/terrapod/v1/workspaces/%s/remote-state-consumers", workspaceID), body)
	return err
}

// readRemoteStateConsumers reads the producer's outbound consumer
// workspace IDs and returns them as a terraform Set<string>. A null
// Set is returned on error (caller decides whether to surface it).
func readRemoteStateConsumers(ctx context.Context, c *terrapod.Client, workspaceID string) (types.Set, diag.Diagnostics) {
	var diags diag.Diagnostics
	url := fmt.Sprintf("/api/terrapod/v1/workspaces/%s/remote-state-consumers?filter[remote-state-consumer][type]=outbound", workspaceID)
	data, err := c.Get(ctx, url)
	if err != nil {
		diags.AddError("Failed to read remote_state_consumers", err.Error())
		return types.SetNull(types.StringType), diags
	}
	items, err := terrapod.ParseResourceList(data)
	if err != nil {
		diags.AddError("Failed to parse remote_state_consumers response", err.Error())
		return types.SetNull(types.StringType), diags
	}
	vals := make([]attr.Value, 0, len(items))
	for i := range items {
		if v := terrapod.GetRelationshipID(&items[i], "consumer"); v != "" {
			vals = append(vals, types.StringValue(v))
		}
	}
	s, d := types.SetValue(types.StringType, vals)
	diags.Append(d...)
	return s, diags
}

// applyConsumersFromPlan PUTs the plan's remote_state_consumers to the
// server iff the attribute is non-null. Null in plan ⇒ unmanaged here
// (server side left intact). Empty set ⇒ explicit "remove all".
func applyConsumersFromPlan(ctx context.Context, c *terrapod.Client, workspaceID string, plan types.Set) error {
	if plan.IsNull() || plan.IsUnknown() {
		return nil
	}
	elems := plan.Elements()
	ids := make([]string, 0, len(elems))
	for _, v := range elems {
		s, ok := v.(types.String)
		if !ok || s.IsNull() || s.IsUnknown() {
			continue
		}
		ids = append(ids, s.ValueString())
	}
	return putRemoteStateConsumers(ctx, c, workspaceID, ids)
}
