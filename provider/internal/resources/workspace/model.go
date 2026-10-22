// Package workspace implements the terrapod_workspace resource.
//
// API Contract (Terrapod API ↔ Terraform Provider):
//
//	JSON:API type: "workspaces"
//	ID prefix: "ws-"
//	Create:  POST   /api/v2/organizations/default/workspaces
//	Read:    GET    /api/v2/workspaces/{id}
//	Update:  PATCH  /api/v2/workspaces/{id}
//	Delete:  DELETE /api/v2/workspaces/{id}
//	By name: GET    /api/v2/organizations/default/workspaces/{name}
//	List:    GET    /api/v2/organizations/default/workspaces
//
// Attribute mapping (JSON:API attribute → Terraform schema attribute):
//
//	"name"                              → name                (string, required, supports rename)
//	"execution-mode"                    → execution_mode      (string, optional, default "local")
//	"auto-apply"                        → auto_apply          (bool,   optional, default false)
//	"execution-backend"                 → execution_backend   (string, optional; defaults to "terraform" on the Terraform engine, server-assigned on Pulumi)
//	"engine"                            → engine              (string, read-only)
//	"pulumi-bind-plan"                  → pulumi_bind_plan    (bool,   optional+computed)
//	"engine-version"                    → engine_version      (string, optional; `terraform_version` is its deprecated alias)
//	"terraform-version"                 → terraform_version   (string, optional, deprecated)
//	"ansible-version"                   → ansible_version     (string, optional; empty inherits the deployment default)
//	"config-directory"                  → config_directory    (string, optional; NULL and "" differ — see below)
//	"inventory-paths"                   → inventory_paths     (list,   optional+computed; ordered)
//	"ansible-collections"               → ansible_collections (list of {name, version})
//	"default-playbook"                  → default_playbook    (string, optional+computed)
//	"default-limit"                     → default_limit       (string, optional+computed)
//	"inventory-include-platform"        → inventory_include_platform (bool, optional+computed; defaults true)
//	"inventory-ignore-paths"            → inventory_ignore_paths (list, optional+computed)
//	"working-directory"                 → working_directory   (string, optional)
//	"parallelism"                       → parallelism
//	"resource-cpu"                      → resource_cpu        (string, optional, default "1")
//	"resource-memory"                   → resource_memory     (string, optional, default "2Gi")
//	"labels"                            → labels              (map,    optional)
//	"vcs-repo-url"                      → vcs_repo_url        (string, optional)
//	"vcs-branch"                        → vcs_branch          (string, optional)
//	"agent-pool-id"                     → agent_pool_id       (string, optional)
//	"agent-pool-ids"                    → agent_pool_ids      (list, optional+computed)
//	"var-files"                         → var_files           (list,   optional)
//	"trigger-prefixes"                  → trigger_prefixes    (list,   optional)
//	"oidc-audiences"                    → oidc_audiences      (map of lists, optional+computed)
//	"drift-detection-enabled"           → drift_detection_enabled (bool, optional)
//	"drift-detection-interval-seconds"  → drift_detection_interval_seconds (int, optional)
//
// Read-only attributes:
//
//	"owner-email"                       → owner_email         (string, computed)
//	"drift-status"                      → drift_status        (string, computed)
//	"drift-last-checked-at"             → drift_last_checked_at (string, computed)
//	"locked"                            → locked              (bool,   computed)
//	"lock-reason"                       → lock_reason         (string, computed)
//	"locked-by"                         → locked_by           (string, computed)
//	"created-at"                        → created_at          (string, computed)
//	"updated-at"                        → updated_at          (string, computed)
//
// Relationships:
//
//	"vcs-connection" → vcs_connection_id (string, optional, to-one)
//
// Import: by workspace name (resolved via GET by-name endpoint).
package workspace

import (
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// audienceElemType is the element type of oidc_audiences (#1901): a LIST of
// audiences per provider configuration, never a bare string.
//
// One audience is the common case and is still a one-element list. A list of
// several is a deliberate "these are interchangeable for this target"
// statement, and some federation targets refuse a multi-valued `aud` outright —
// so the shape must not collapse to a scalar on the one-entry case, or the
// multi-entry one stops being expressible.
var audienceElemType = types.ListType{ElemType: types.StringType}

// workspaceModel maps the Terraform schema to Go types.
type workspaceModel struct {
	ID types.String `tfsdk:"id"`

	// Writable attributes
	Name                        types.String `tfsdk:"name"`
	ExecutionMode               types.String `tfsdk:"execution_mode"`
	AutoApply                   types.Bool   `tfsdk:"auto_apply"`
	AutoApplyMode               types.String `tfsdk:"auto_apply_mode"`
	ExecutionBackend            types.String `tfsdk:"execution_backend"`
	Engine                      types.String `tfsdk:"engine"`
	PulumiBindPlan              types.Bool   `tfsdk:"pulumi_bind_plan"`
	EngineVersion               types.String `tfsdk:"engine_version"`
	TerraformVersion            types.String `tfsdk:"terraform_version"`
	TerragruntEnabled           types.Bool   `tfsdk:"terragrunt_enabled"`
	TerragruntVersion           types.String `tfsdk:"terragrunt_version"`
	AnsibleVersion              types.String `tfsdk:"ansible_version"`
	AnsibleExecutionEnvironment types.String `tfsdk:"ansible_execution_environment"`
	// Which operations this workspace has (#1966). Two independent booleans
	// rather than one parsed value, because the states are genuinely
	// independent and a delimited string has to be kept in step across the
	// API, this provider, MCP and the UI -- which is how it eventually means
	// a third thing. The server defaults PlanApplyEnabled TRUE and
	// ConfigureEnabled FALSE, and refuses the one combination two booleans
	// permit and nobody wants: both false, which can never produce a run.
	PlanApplyEnabled types.Bool `tfsdk:"plan_apply_enabled"`
	ConfigureEnabled types.Bool `tfsdk:"configure_enabled"`
	// How a configure STARTS (#1974). Both default false on the server, both
	// need ConfigureEnabled to mean anything, and neither has a cross-field
	// invariant of its own -- a trigger declared on a workspace whose configure
	// operation is not (yet) on is inert rather than incoherent, which is what
	// lets a configuration declare them in any order.
	AutoConfigureEnabled types.Bool `tfsdk:"auto_configure_enabled"`
	ConfigureAfterApply  types.Bool `tfsdk:"configure_after_apply"`
	// The ansible configure operation's roots and run defaults (#1929).
	//
	// ConfigDirectory is Optional+Computed with UseStateForUnknown, exactly
	// like WorkingDirectory, and that is the whole of #1966's change here. It
	// used to be Optional-only to keep null and "" apart, because null meant
	// "this workspace has no configure operation" and "" meant the repository
	// root -- one field answering two questions. ConfigureEnabled answers the
	// first one now, so this is an ordinary non-nullable path where "" is the
	// configuration version's root. The server stores and returns a concrete
	// string and REFUSES a JSON null, so Optional-only would plan an omitted
	// attribute as null against a server answer of "" and fail every apply of
	// every workspace that does not set it with "Provider produced
	// inconsistent result after apply".
	ConfigDirectory               types.String `tfsdk:"config_directory"`
	InventoryPaths                types.List   `tfsdk:"inventory_paths"`
	AnsibleCollections            types.List   `tfsdk:"ansible_collections"`
	DefaultPlaybook               types.String `tfsdk:"default_playbook"`
	DefaultLimit                  types.String `tfsdk:"default_limit"`
	InventoryIncludePlatform      types.Bool   `tfsdk:"inventory_include_platform"`
	InventoryIgnorePaths          types.List   `tfsdk:"inventory_ignore_paths"`
	WorkingDirectory              types.String `tfsdk:"working_directory"`
	ResourceCPU                   types.String `tfsdk:"resource_cpu"`
	EEResourceCPU                 types.String `tfsdk:"ee_resource_cpu"`
	EEResourceMemory              types.String `tfsdk:"ee_resource_memory"`
	Parallelism                   types.Int64  `tfsdk:"parallelism"`
	ResourceMemory                types.String `tfsdk:"resource_memory"`
	Labels                        types.Map    `tfsdk:"labels"`
	VCSRepoURL                    types.String `tfsdk:"vcs_repo_url"`
	VCSBranch                     types.String `tfsdk:"vcs_branch"`
	VCSConnectionID               types.String `tfsdk:"vcs_connection_id"`
	VCSWorkflow                   types.String `tfsdk:"vcs_workflow"`
	AutoMerge                     types.Bool   `tfsdk:"auto_merge"`
	AutoMergeStrategy             types.String `tfsdk:"auto_merge_strategy"`
	AgentPoolID                   types.String `tfsdk:"agent_pool_id"`
	AgentPoolIDs                  types.List   `tfsdk:"agent_pool_ids"`
	AgentPoolNames                types.List   `tfsdk:"agent_pool_names"`
	VarFiles                      types.List   `tfsdk:"var_files"`
	TriggerPrefixes               types.List   `tfsdk:"trigger_prefixes"`
	DriftIgnoreRules              types.List   `tfsdk:"drift_ignore_rules"`
	DriftDetectionEnabled         types.Bool   `tfsdk:"drift_detection_enabled"`
	DriftDetectionIntervalSeconds types.Int64  `tfsdk:"drift_detection_interval_seconds"`
	SecurityScanEnforcement       types.String `tfsdk:"security_scan_enforcement"`
	SecurityScanEngine            types.String `tfsdk:"security_scan_engine"`
	SecurityScanSeverityThreshold types.String `tfsdk:"security_scan_severity_threshold"`
	SecurityScanSkipRules         types.List   `tfsdk:"security_scan_skip_rules"`
	OIDCAudiences                 types.Map    `tfsdk:"oidc_audiences"`
	PlanExpirySeconds             types.Int64  `tfsdk:"plan_expiry_seconds"`
	DebugMode                     types.Bool   `tfsdk:"debug_mode"`
	AllowForkPRPlans              types.Bool   `tfsdk:"allow_fork_pr_plans"`
	AISummaryMode                 types.String `tfsdk:"ai_summary_mode"`
	AIPolicyMode                  types.String `tfsdk:"ai_policy_mode"`
	AISummaryContext              types.String `tfsdk:"ai_summary_context"`
	SlackChannel                  types.String `tfsdk:"slack_channel"`

	// Set of workspace IDs authorized to read this workspace's state
	// via `terraform_remote_state` (#344). Producer-controlled
	// allowlist; mutations require admin/write on this (producer)
	// workspace. Optional + Computed: leave null to opt out of
	// managing the set here (server side is left intact); set to []
	// to explicitly remove all consumers. See #348.
	RemoteStateConsumers types.Set `tfsdk:"remote_state_consumers"`

	// Read-only attributes
	OwnerEmail         types.String `tfsdk:"owner_email"`
	DriftStatus        types.String `tfsdk:"drift_status"`
	DriftLastCheckedAt types.String `tfsdk:"drift_last_checked_at"`
	DriftLatestRunID   types.String `tfsdk:"drift_latest_run_id"`
	StateDiverged      types.Bool   `tfsdk:"state_diverged"`
	LifecycleState     types.String `tfsdk:"lifecycle_state"`
	LifecycleReason    types.String `tfsdk:"lifecycle_reason"`
	VCSLastPolledAt    types.String `tfsdk:"vcs_last_polled_at"`
	VCSLastAttemptedAt types.String `tfsdk:"vcs_last_attempted_at"`
	VCSLastError       types.String `tfsdk:"vcs_last_error"`
	VCSLastErrorAt     types.String `tfsdk:"vcs_last_error_at"`
	AgentPoolName      types.String `tfsdk:"agent_pool_name"`
	VCSConnectionName  types.String `tfsdk:"vcs_connection_name"`
	Locked             types.Bool   `tfsdk:"locked"`
	LockReason         types.String `tfsdk:"lock_reason"`
	LockedBy           types.String `tfsdk:"locked_by"`
	CreatedAt          types.String `tfsdk:"created_at"`
	UpdatedAt          types.String `tfsdk:"updated_at"`
}
