package terrapod

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
)

// Workspace is the decoded form of one Terrapod workspace resource.
// Field tags mirror the JSON:API attribute names so callers can also
// (de)serialise directly when convenient.
//
// Nullable string fields (EngineVersion, VCSRepoURL, VCSBranch,
// AgentPoolID, etc.) are returned as empty strings rather than nil
// pointers — Terrapod treats empty and null equivalently here, and the
// pointer-vs-empty distinction adds friction without information.
//
// DriftDetectionIntervalSeconds uses *int64 because zero is a
// meaningful value the server may return (disabled-by-explicit-zero
// vs. unset). Same logic for any future field where 0 is a legal
// non-default value.
// AnsibleCollection is one entry in a workspace's declared collection list
// (#1931). It is ansible's own `requirements.yml` entry shape, minus the parts
// Terrapod does not accept: no `source` (every collection is fetched from this
// deployment's own Galaxy surface) and no `type` (a git or tarball requirement
// would bypass the registry and its RBAC).
//
// Version is optional and means "whatever resolves", exactly as it does in
// ansible's own file.
type AnsibleCollection struct {
	Name    string `json:"name"`
	Version string `json:"version,omitempty"`
}

type Workspace struct {
	ID               string `json:"id"`
	Name             string `json:"name"`
	ExecutionMode    string `json:"execution-mode"`
	ExecutionBackend string `json:"execution-backend,omitempty"`

	// Engine is the execution engine family this workspace belongs to —
	// "terraform" today. Distinct from ExecutionBackend, which picks the
	// binary *within* the Terraform engine (tofu or terraform).
	//
	// The TFE-compatible /api/v2 surface is filtered to Terraform, so a
	// workspace on another engine is never visible to a terraform/tofu
	// client and its responses are free to use that engine's own vocabulary.
	Engine string `json:"engine,omitempty"`
	// PulumiBindPlan: Pulumi workspaces only (#1553) — the update is bound to
	// the approved preview's saved plan. False by default.
	PulumiBindPlan bool `json:"pulumi-bind-plan"`
	AutoApply      bool `json:"auto-apply"`
	// AutoApplyMode is the conditional auto-apply setting (#1274):
	// "never", "always", "create" or "create_update". AutoApply stays the
	// boolean projection — true whenever the workspace applies unattended
	// at all — so code that only reads it keeps working unchanged.
	AutoApplyMode string `json:"auto-apply-mode"`
	// EngineVersion pins the version of whichever engine this workspace runs.
	// TerraformVersion is the same version under its original name — the API
	// returns both, always equal, because go-tfe reads "terraform-version" and
	// so tofu/terraform/tfci depend on it (#1559). That name is permanent, not
	// a deprecation window; only the spelling a new caller should reach for has
	// changed. Same shape as Variable's Structured/HCL pair.
	EngineVersion    string `json:"engine-version,omitempty"`
	TerraformVersion string `json:"terraform-version,omitempty"`
	// TerragruntEnabled wraps tofu/terraform with terragrunt for agent-mode
	// runs; TerragruntVersion pins the terragrunt CLI version (partial like
	// "1.0" allowed — the binary cache resolves it). See docs/terragrunt.md.
	TerragruntEnabled bool   `json:"terragrunt-enabled"`
	TerragruntVersion string `json:"terragrunt-version,omitempty"`
	WorkingDirectory  string `json:"working-directory,omitempty"`
	ResourceCPU       string `json:"resource-cpu,omitempty"`
	// The Ansible execution environment's own requests (#2154), separate from
	// the orchestrator's above because the two containers do different work.
	// Empty means the deployment default, as for AnsibleVersion -- and
	// `omitempty` therefore has the same consequence it has there: omitting
	// the field on an update leaves the stored value alone rather than
	// clearing it back to the default.
	EEResourceCPU    string `json:"ee-resource-cpu,omitempty"`
	EEResourceMemory string `json:"ee-resource-memory,omitempty"`
	Parallelism      int64  `json:"parallelism,omitempty"`
	AnsibleVersion   string `json:"ansible-version,omitempty"`
	// AnsibleExecutionEnvironment names one of the deployment's own execution
	// environments by LABEL (#2154), and OVERRIDES AnsibleVersion when set --
	// so a workspace carrying one ignores its line entirely.
	//
	// A label rather than an image reference, and a separate field rather than
	// an overloaded AnsibleVersion for a measured reason: the server screens
	// that field only for pre-releases, so a label would pass it -- and a label
	// named "2.21" would shadow a real ansible-core line with no way to tell.
	//
	// Available labels come from ListAnsibleExecutionEnvironments; naming one
	// the deployment has not configured is refused at the write with a 422.
	AnsibleExecutionEnvironment string `json:"ansible-execution-environment,omitempty"`
	// AnsibleCollections are the collections a configure installs, declared on
	// the platform (#1931). Installed AFTER the configuration's own
	// requirements file and with --force, so an entry here overrides the
	// repository's build of that collection and leaves the rest standing.
	//
	// `!= nil` is how an empty list travels on a write, as with InventoryPaths:
	// `[]` means "declare none here" and has to be distinguishable from "do
	// not touch this".
	AnsibleCollections []AnsibleCollection `json:"ansible-collections,omitempty"`
	ResourceMemory     string              `json:"resource-memory,omitempty"`
	// PlanApplyEnabled and ConfigureEnabled are which operations this
	// workspace HAS (#1966). Independent, and the server refuses both false.
	//
	// No `omitempty` on either, as on AutoApply above: a false must survive a
	// round trip, and `omitempty` drops it.
	//
	// Not served on /api/tfe/v2 — that surface is engine-filtered to Terraform
	// and go-tfe switches on a fixed attribute set — so both read as false
	// through the compatibility route whatever they hold. Note the name
	// `operations` was never available there in any case: it is TFE's own
	// boolean for whether the workspace uses remote operations.
	PlanApplyEnabled bool `json:"plan-apply-enabled"`
	ConfigureEnabled bool `json:"configure-enabled"`
	// AutoConfigureEnabled and ConfigureAfterApply are HOW a configure starts
	// (#1974). Both opt-in, both default false, and neither means anything
	// without ConfigureEnabled.
	//
	// AutoConfigureEnabled: a push that changes the workspace's Ansible config
	// root queues a configure, running the declared default playbook against
	// the declared default limit -- a push carries no run options, which is
	// also what bounds it.
	//
	// ConfigureAfterApply: an apply on this workspace is followed by a
	// configure ON THE SAME RUN. A workspace setting rather than a run
	// attribute because the terraform/tofu CLI has no way to ask for one, and
	// a remote execution streaming the configure's logs is the whole reason it
	// exists. A destroy apply never chains one.
	//
	// No `omitempty`, as on the pair above and for the same reason.
	//
	// There is deliberately no scheduled configure. An operator who wants one
	// drives CreateRun from their own scheduler.
	AutoConfigureEnabled bool `json:"auto-configure-enabled"`
	ConfigureAfterApply  bool `json:"configure-after-apply"`
	// ConfigureAllHosts forces a DERIVED configure -- one an inventory change
	// or an apply produced, rather than one somebody asked for with a limit --
	// to target every host instead of only the ones that changed (#2229).
	//
	// Narrowing is the default, and it is conditioned on what justifies
	// breadth: new playbook or role code has to reach every host, while a
	// changed HOST is no reason to touch the others. So a derived configure
	// narrows when the Ansible code under ConfigDirectory is unchanged since
	// the last successful configure, and goes wide when it moved.
	//
	// This flag is the override and it WIDENS. That direction is deliberate:
	// a flag someone forgets leaves them configuring more, not less.
	ConfigureAllHosts bool `json:"configure-all-hosts"`
	// ConfigDirectory is the directory the ansible configure operation runs in
	// (#1929), relative to the configuration version's root: its fetch scope
	// and the resolver's and executor's working directory.
	//
	// A plain string, with "" meaning that root -- WorkingDirectory's exact
	// shape. It used to be a pointer whose nil meant "no configure operation"
	// (#1929), which made one field answer both where the operation runs and
	// whether there is one; the second question is ConfigureEnabled above
	// (#1966).
	//
	// Not served on /api/tfe/v2, for the reason given above, so a workspace
	// read through the compatibility route reads this as "" whatever it holds.
	ConfigDirectory string `json:"config-directory,omitempty"`
	// InventoryPaths are the inventory sources, each a file or a directory
	// relative to the repository root. ORDERED: the order IS the precedence
	// ansible applies, so it is not a set.
	InventoryPaths []string `json:"inventory-paths,omitempty"`
	// DefaultPlaybook is the playbook a configure run executes unless the run
	// overrides it, the way `vcs-ref` overrides `vcs-branch`.
	DefaultPlaybook string `json:"default-playbook,omitempty"`
	// DefaultLimit is the default `--limit` for a configure run. Empty means
	// the whole inventory, which is ansible's own default.
	DefaultLimit string `json:"default-limit,omitempty"`
	// InventoryIncludePlatform is whether the rows declared through the
	// inventory API contribute to the resolution. **Defaults to true**, so an
	// absent value is NOT false — see the read in workspaceFromResource.
	InventoryIncludePlatform bool `json:"inventory-include-platform"`
	// InventoryIgnorePaths are paths excluded from the resolution.
	InventoryIgnorePaths []string `json:"inventory-ignore-paths,omitempty"`
	// OIDCAudiences is the per-workspace cloud identity opt-in (#1901), keyed
	// on the provider configuration a token is for: `aws`, or `aws.west` for
	// one aliased configuration. Empty means this workspace mints nothing and
	// its runs authenticate with the agent pool's own identity, exactly as
	// before.
	//
	// **This is the MERGED view, not the workspace's own override.** The server
	// merges the workspace's map over the deployment's catalogue per key, so a
	// key the workspace does not set is still present here with the
	// deployment's value. A consumer writing this field back wholesale would
	// therefore promote every inherited entry into an override.
	//
	// An audience is an opaque string the federation target chose, so the
	// server stores it byte-for-byte and the SDK passes it through unaltered —
	// there is nothing cloud-specific here, and nothing to canonicalise.
	OIDCAudiences   map[string][]string `json:"oidc-audiences,omitempty"`
	VCSRepoURL      string              `json:"vcs-repo-url,omitempty"`
	VCSBranch       string              `json:"vcs-branch,omitempty"`
	VCSWorkflow     string              `json:"vcs-workflow,omitempty"`
	VCSConnectionID string              `json:"vcs-connection-id,omitempty"` // resolved from `vcs-connection` relationship
	AgentPoolID     string              `json:"agent-pool-id,omitempty"`
	AgentPoolIDs    []string            `json:"agent-pool-ids,omitempty"` // flat pool set (#1085); AgentPoolID is element 0
	// AgentPoolNames is read-only: the pools' names, positionally matching
	// AgentPoolIDs. Served so a consumer can render a pool without fetching the
	// whole pool list purely to turn ids into labels.
	AgentPoolNames    []string `json:"agent-pool-names,omitempty"`
	AutoMerge         bool     `json:"auto-merge"`
	AutoMergeStrategy string   `json:"auto-merge-strategy,omitempty"`
	OwnerEmail        string   `json:"owner-email,omitempty"`
	Locked            bool     `json:"locked"`
	// LockReason and LockedBy are read-only (#1705): why the workspace is
	// locked and who took the lock. Empty when unlocked, or when the lock was
	// taken without a reason (or by a server that predates them).
	LockReason      string            `json:"lock-reason,omitempty"`
	LockedBy        string            `json:"locked-by,omitempty"`
	Labels          map[string]string `json:"labels,omitempty"`
	VarFiles        []string          `json:"var-files,omitempty"`
	TriggerPrefixes []string          `json:"trigger-prefixes,omitempty"`
	// DriftIgnoreRules is a list of resource-address-plus-attribute-path
	// glob patterns suppressed by the drift-result classifier (#482).
	// Empty list (default) means classic behaviour: every plan diff
	// counts as drift. See docs/api-reference.md for the rule grammar.
	DriftIgnoreRules              []string `json:"drift-ignore-rules,omitempty"`
	DriftDetectionEnabled         bool     `json:"drift-detection-enabled"`
	DriftDetectionIntervalSeconds *int64   `json:"drift-detection-interval-seconds,omitempty"`
	// Security-scan (Checkov/Trivy IaC misconfig scanning, #1036): enforcement
	// off|advisory|enforced, engine checkov|trivy|both, severity threshold
	// critical|high|medium|low, and a list of scanner rule-ids to skip.
	SecurityScanEnforcement       string   `json:"security-scan-enforcement,omitempty"`
	SecurityScanEngine            string   `json:"security-scan-engine,omitempty"`
	SecurityScanSeverityThreshold string   `json:"security-scan-severity-threshold,omitempty"`
	SecurityScanSkipRules         []string `json:"security-scan-skip-rules,omitempty"`
	// PlanExpirySeconds is the per-workspace plan-expiry TTL (#646); nil =
	// disabled (the default). An apply-capable plan older than this is
	// auto-discarded and must be re-planned.
	PlanExpirySeconds  *int64 `json:"plan-expiry-seconds,omitempty"`
	DriftStatus        string `json:"drift-status,omitempty"`
	DriftLastCheckedAt string `json:"drift-last-checked-at,omitempty"`
	// DriftLatestRunID is the ID (prefixed `run-…`) of the drift run that
	// produced the current DriftStatus, or "" when drift has never run or
	// was just cleared by a successful apply. Lets consumers link the
	// status badge to the actual run.
	DriftLatestRunID string `json:"drift-latest-run-id,omitempty"`
	// StateDiverged is set when an apply Job succeeded but uploading the
	// resulting state to Terrapod failed — the workspace's recorded state
	// is now out of sync with reality.
	StateDiverged bool `json:"state-diverged"`
	// LifecycleState tracks autodiscovery-managed workspaces:
	// "active" | "pending_deletion" | "archived".
	LifecycleState string `json:"lifecycle-state,omitempty"`
	// LifecycleReason is a human-readable explanation of LifecycleState
	// (e.g. "directory 'accounts/x' removed on 'main'"). Empty for active.
	LifecycleReason string `json:"lifecycle-reason,omitempty"`
	// VCSLastPolledAt is the timestamp of the most recent successful VCS
	// poll cycle for this workspace.
	VCSLastPolledAt string `json:"vcs-last-polled-at,omitempty"`
	// VCSLastAttemptedAt is the timestamp of the most recent poll ATTEMPT,
	// successful or not. VCSLastPolledAt only advances on success, so the gap
	// between the two is what identifies a workspace whose polls are failing
	// rather than one that is simply not due yet.
	VCSLastAttemptedAt string `json:"vcs-last-attempted-at,omitempty"`
	// VCSLastError is the last error from a VCS poll attempt (auth
	// failure, repo gone, etc.). Empty when the last poll succeeded.
	VCSLastError string `json:"vcs-last-error,omitempty"`
	// VCSLastErrorAt is the timestamp of VCSLastError.
	VCSLastErrorAt string `json:"vcs-last-error-at,omitempty"`
	// AgentPoolName is the human-readable name of the assigned agent
	// pool, server-derived from AgentPoolID. Empty when no pool is set.
	AgentPoolName string `json:"agent-pool-name,omitempty"`
	// VCSConnectionName is the human-readable name of the assigned VCS
	// connection, server-derived from VCSConnectionID. Empty when none.
	VCSConnectionName string `json:"vcs-connection-name,omitempty"`
	// DebugMode holds this workspace's failed runner pods open for
	// inspection (#1764). How long is the deployment's setting, not the
	// workspace's — the pod keeps the run's credentials for that window.
	DebugMode bool `json:"debug-mode"`
	// AllowForkPRPlans permits a speculative plan for a pull request opened
	// from a fork (GHSA-gp5w-76rw-c452). Off by default: such a plan runs
	// the fork author's code with this workspace's credentials, and the
	// author has neither write access nor the ability to merge. Pull requests
	// from branches in the repository itself are unaffected.
	AllowForkPRPlans bool `json:"allow-fork-pr-plans"`
	// AISummaryMode is the three-state per-workspace override (#401):
	//   "default"  → follow the deployment-wide ai_summary.enabled flag
	//   "enabled"  → always summarise (no-op when global is off)
	//   "disabled" → never summarise this workspace's plans
	AISummaryMode string `json:"ai-summary-mode,omitempty"`
	// AIPolicyMode is the per-workspace override for the AI policy GATE
	// (#1766), with the same three states as AISummaryMode. Note the one
	// asymmetry: "disabled" opts out of an ADVISORY verdict only. A mandatory
	// gate ignores it, because a fleet-wide blocking control any workspace
	// admin could switch off would not be a control.
	AIPolicyMode string `json:"ai-policy-mode,omitempty"`
	// AISummaryContext is workspace-specific facts added on top of the
	// deployment-wide fleet_context when the summariser builds its prompt.
	AISummaryContext string `json:"ai-summary-context,omitempty"`
	// SlackChannel is the opt-in Slack channel this workspace's run
	// notifications post to (#556). Empty means the workspace is silent.
	SlackChannel string `json:"slack-channel,omitempty"`
	CreatedAt    string `json:"created-at,omitempty"`
	UpdatedAt    string `json:"updated-at,omitempty"`
}

// CreateWorkspaceRequest is the input shape for Client.CreateWorkspace.
// Only Name is required by Terrapod; every other field is optional and
// either takes a server-side default or is updateable later.
//
// To produce a JSON:API body the SDK marshals from this struct rather
// than a free-form map so callers get type safety and the Terrapod
// schema stays singular-sourced in this file.
type CreateWorkspaceRequest struct {
	Name string `json:"name"`
	// Engine names the execution engine family — "terraform" (the default when
	// empty) or another engine this deployment enables. Setting it sends the
	// create to the native surface, which is the only one that can express an
	// engine; the TFE-compatible surface is Terraform-only by design and a
	// client there could not see a workspace belonging to another engine.
	//
	// Workspaces are created here, or in the UI, or with the Terraform provider
	// — never by an engine's own CLI (#1535).
	Engine string `json:"engine,omitempty"`
	// PulumiBindPlan (#1553); the API refuses true for non-Pulumi engines.
	PulumiBindPlan   *bool  `json:"pulumi-bind-plan,omitempty"`
	ExecutionMode    string `json:"execution-mode,omitempty"`
	ExecutionBackend string `json:"execution-backend,omitempty"`
	AutoApply        *bool  `json:"auto-apply,omitempty"`
	// Set AutoApplyMode OR AutoApply, never both — the API rejects the
	// pair with 422 rather than guessing which the caller meant.
	AutoApplyMode *string `json:"auto-apply-mode,omitempty"`
	// Set either; EngineVersion is preferred. TerraformVersion is the same
	// version under the name go-tfe uses, so the API accepts it indefinitely
	// (#1559) — it is not deprecated on the wire. This SDK sends whichever is
	// set under the canonical key, so the two can never disagree in one
	// request; the server rejects a request that sends both and disagrees.
	EngineVersion     string `json:"engine-version,omitempty"`
	TerraformVersion  string `json:"terraform-version,omitempty"`
	TerragruntEnabled *bool  `json:"terragrunt-enabled,omitempty"`
	TerragruntVersion string `json:"terragrunt-version,omitempty"`
	WorkingDirectory  string `json:"working-directory,omitempty"`
	ResourceCPU       string `json:"resource-cpu,omitempty"`
	// The Ansible execution environment's own requests (#2154), separate from
	// the orchestrator's above because the two containers do different work.
	// Empty means the deployment default, as for AnsibleVersion -- and
	// `omitempty` therefore has the same consequence it has there: omitting
	// the field on an update leaves the stored value alone rather than
	// clearing it back to the default.
	EEResourceCPU    string `json:"ee-resource-cpu,omitempty"`
	EEResourceMemory string `json:"ee-resource-memory,omitempty"`
	Parallelism      int64  `json:"parallelism,omitempty"`
	AnsibleVersion   string `json:"ansible-version,omitempty"`
	// AnsibleExecutionEnvironment names one of the deployment's own execution
	// environments by LABEL (#2154), and OVERRIDES AnsibleVersion when set --
	// so a workspace carrying one ignores its line entirely.
	//
	// A label rather than an image reference, and a separate field rather than
	// an overloaded AnsibleVersion for a measured reason: the server screens
	// that field only for pre-releases, so a label would pass it -- and a label
	// named "2.21" would shadow a real ansible-core line with no way to tell.
	//
	// Available labels come from ListAnsibleExecutionEnvironments; naming one
	// the deployment has not configured is refused at the write with a 422.
	AnsibleExecutionEnvironment string `json:"ansible-execution-environment,omitempty"`
	// AnsibleCollections are the collections a configure installs, declared on
	// the platform (#1931). Installed AFTER the configuration's own
	// requirements file and with --force, so an entry here overrides the
	// repository's build of that collection and leaves the rest standing.
	//
	// `!= nil` is how an empty list travels on a write, as with InventoryPaths:
	// `[]` means "declare none here" and has to be distinguishable from "do
	// not touch this".
	AnsibleCollections []AnsibleCollection `json:"ansible-collections,omitempty"`
	ResourceMemory     string              `json:"resource-memory,omitempty"`
	// PlanApplyEnabled and ConfigureEnabled declare which operations the new
	// workspace has (#1966). POINTERS, like AutoApply above and for the same
	// reason: a plain bool's false is indistinguishable from unset, and on
	// PlanApplyEnabled -- whose server-side default is TRUE -- that failure
	// would be silent and in the dangerous direction, every caller that left
	// the field alone disabling plan/apply.
	//
	//	nil        -- omitted; the server default applies (plan/apply on,
	//	              configure off)
	//	&true      -- explicitly on
	//	&false      -- explicitly off
	//
	// The server refuses both false with a 422.
	PlanApplyEnabled *bool `json:"plan-apply-enabled,omitempty"`
	ConfigureEnabled *bool `json:"configure-enabled,omitempty"`
	// AutoConfigureEnabled and ConfigureAfterApply say how a configure starts
	// on the new workspace (#1974). POINTERS for the same reason as the pair
	// above, though the direction is gentler here: both server-side defaults
	// are FALSE, so a caller that leaves them alone gets no triggers rather
	// than a surprise.
	AutoConfigureEnabled *bool `json:"auto-configure-enabled,omitempty"`
	ConfigureAfterApply  *bool `json:"configure-after-apply,omitempty"`
	// ConfigureAllHosts makes every derived configure on the new workspace
	// sweep the whole inventory rather than narrowing to the hosts an
	// inventory change affected (#2229). A POINTER for the reason the pair
	// above gives; the server-side default is FALSE, which narrows.
	ConfigureAllHosts *bool `json:"configure-all-hosts,omitempty"`
	// ConfigDirectory says where the ansible configure operation is rooted
	// (#1929), relative to the configuration version's root. It does NOT turn
	// the operation on -- ConfigureEnabled does (#1966).
	//
	// A plain string now that "" and omitted mean the same thing: both leave
	// the operation rooted at that root, which is the server-side default. It
	// was a three-state pointer while nil meant "no configure operation".
	ConfigDirectory string `json:"config-directory,omitempty"`
	// InventoryPaths are the inventory sources, each a file or a directory
	// relative to the repository root. ORDERED -- the order is the precedence
	// ansible applies. A nil slice is omitted; an empty non-nil slice
	// (`[]string{}`) is sent, which declares no sources.
	InventoryPaths []string `json:"inventory-paths,omitempty"`
	// DefaultPlaybook and DefaultLimit are the configure run's defaults, each
	// overridable per run. Empty is the server-side default for both, so an
	// empty value here is omitted rather than sent.
	DefaultPlaybook string `json:"default-playbook,omitempty"`
	DefaultLimit    string `json:"default-limit,omitempty"`
	// InventoryIncludePlatform is whether rows declared through the inventory
	// API contribute to the resolution. A POINTER because the server-side
	// default is **true**: a plain bool could not send false, so the one
	// request that resolves the git source alone would silently not apply.
	InventoryIncludePlatform *bool `json:"inventory-include-platform,omitempty"`
	// InventoryIgnorePaths are paths excluded from the resolution. Same
	// nil-versus-empty rule as InventoryPaths.
	InventoryIgnorePaths []string `json:"inventory-ignore-paths,omitempty"`
	// OIDCAudiences is the per-workspace cloud identity override (#1901), keyed
	// on the provider configuration: `aws`, or `aws.west` for one aliased
	// configuration. It is MERGED over the deployment's catalogue per key, so
	// setting one key does not restate the rest and removing one falls back to
	// the deployment's value.
	//
	// A nil map leaves the server-side value alone; an empty non-nil map
	// (`map[string][]string{}`) clears it, which is how a workspace drops every
	// override and inherits the catalogue wholesale.
	//
	// A key present with an EMPTY list is refused (422). "No audiences for this
	// target" is expressed by removing the key, which falls back to the
	// deployment's configured value — an empty list would otherwise be an
	// opt-out that looks identical to a typo.
	OIDCAudiences                 map[string][]string `json:"oidc-audiences,omitempty"`
	VCSRepoURL                    string              `json:"vcs-repo-url,omitempty"`
	VCSBranch                     string              `json:"vcs-branch,omitempty"`
	VCSWorkflow                   string              `json:"vcs-workflow,omitempty"`
	VCSConnectionID               string              `json:"-"` // → relationship, not attribute
	AgentPoolID                   string              `json:"agent-pool-id,omitempty"`
	AgentPoolIDs                  []string            `json:"agent-pool-ids,omitempty"` // whole pool set; mutually exclusive with AgentPoolID (422)
	AutoMerge                     *bool               `json:"auto-merge,omitempty"`
	AutoMergeStrategy             string              `json:"auto-merge-strategy,omitempty"`
	OwnerEmail                    string              `json:"owner-email,omitempty"`
	Labels                        map[string]string   `json:"labels,omitempty"`
	VarFiles                      []string            `json:"var-files,omitempty"`
	TriggerPrefixes               []string            `json:"trigger-prefixes,omitempty"`
	DriftIgnoreRules              []string            `json:"drift-ignore-rules,omitempty"`
	DriftDetectionEnabled         *bool               `json:"drift-detection-enabled,omitempty"`
	DriftDetectionIntervalSeconds *int64              `json:"drift-detection-interval-seconds,omitempty"`
	SecurityScanEnforcement       string              `json:"security-scan-enforcement,omitempty"`
	SecurityScanEngine            string              `json:"security-scan-engine,omitempty"`
	SecurityScanSeverityThreshold string              `json:"security-scan-severity-threshold,omitempty"`
	SecurityScanSkipRules         []string            `json:"security-scan-skip-rules,omitempty"`
	PlanExpirySeconds             *int64              `json:"plan-expiry-seconds,omitempty"`
	// AISummaryMode is the three-state per-workspace override (#401):
	// "default" | "enabled" | "disabled". Empty string omits the field
	// (server-side default applies — "default").
	// DebugMode holds this workspace's failed runner pods open for inspection
	// (#1764). A pointer so that "leave it alone" and "turn it off" stay
	// distinguishable -- a bare bool with omitempty cannot express the second.
	DebugMode *bool `json:"debug-mode,omitempty"`
	// AllowForkPRPlans permits a speculative plan for a pull request opened
	// from a fork (GHSA-gp5w-76rw-c452). A pointer for the same reason as
	// DebugMode: a bare bool with omitempty cannot distinguish "leave it
	// alone" from "turn it off", and this one gates whether untrusted code
	// sees the credentials.
	AllowForkPRPlans *bool  `json:"allow-fork-pr-plans,omitempty"`
	AISummaryMode    string `json:"ai-summary-mode,omitempty"`
	// AIPolicyMode is the per-workspace override for the AI policy GATE
	// (#1766), with the same three states as AISummaryMode. Note the one
	// asymmetry: "disabled" opts out of an ADVISORY verdict only. A mandatory
	// gate ignores it, because a fleet-wide blocking control any workspace
	// admin could switch off would not be a control.
	AIPolicyMode string `json:"ai-policy-mode,omitempty"`
	// AISummaryContext is workspace-specific context added to the model
	// prompt. Capped at 4000 chars server-side.
	AISummaryContext string `json:"ai-summary-context,omitempty"`
	// SlackChannel opts this workspace into Slack run notifications (#556)
	// on the given channel. Empty means silent.
	SlackChannel string `json:"slack-channel,omitempty"`
}

// UpdateWorkspaceRequest is the input shape for Client.UpdateWorkspace.
// Mirrors CreateWorkspaceRequest including Name — Terrapod's
// workspace PATCH endpoint accepts a rename, so the SDK exposes it
// here. Pass an empty string to leave the name alone.
//
// Pointer-typed bool fields let callers distinguish "leave alone"
// (nil) from "set to false" (&false). Without that distinction we'd
// flip a workspace's auto-apply to false on every PATCH that didn't
// explicitly set it.
type UpdateWorkspaceRequest struct {
	// PulumiBindPlan (#1553); the API refuses true for non-Pulumi engines.
	PulumiBindPlan   *bool  `json:"pulumi-bind-plan,omitempty"`
	Name             string `json:"name,omitempty"`
	ExecutionMode    string `json:"execution-mode,omitempty"`
	ExecutionBackend string `json:"execution-backend,omitempty"`
	AutoApply        *bool  `json:"auto-apply,omitempty"`
	// Set AutoApplyMode OR AutoApply, never both — the API rejects the
	// pair with 422 rather than guessing which the caller meant.
	AutoApplyMode *string `json:"auto-apply-mode,omitempty"`
	// Set either; EngineVersion is preferred. TerraformVersion is the same
	// version under the name go-tfe uses, so the API accepts it indefinitely
	// (#1559) — it is not deprecated on the wire. This SDK sends whichever is
	// set under the canonical key, so the two can never disagree in one
	// request; the server rejects a request that sends both and disagrees.
	EngineVersion     string `json:"engine-version,omitempty"`
	TerraformVersion  string `json:"terraform-version,omitempty"`
	TerragruntEnabled *bool  `json:"terragrunt-enabled,omitempty"`
	TerragruntVersion string `json:"terragrunt-version,omitempty"`
	WorkingDirectory  string `json:"working-directory,omitempty"`
	ResourceCPU       string `json:"resource-cpu,omitempty"`
	// The Ansible execution environment's own requests (#2154), separate from
	// the orchestrator's above because the two containers do different work.
	// Empty means the deployment default, as for AnsibleVersion -- and
	// `omitempty` therefore has the same consequence it has there: omitting
	// the field on an update leaves the stored value alone rather than
	// clearing it back to the default.
	EEResourceCPU    string `json:"ee-resource-cpu,omitempty"`
	EEResourceMemory string `json:"ee-resource-memory,omitempty"`
	Parallelism      int64  `json:"parallelism,omitempty"`
	AnsibleVersion   string `json:"ansible-version,omitempty"`
	// AnsibleExecutionEnvironment names one of the deployment's own execution
	// environments by LABEL (#2154), and OVERRIDES AnsibleVersion when set --
	// so a workspace carrying one ignores its line entirely.
	//
	// A label rather than an image reference, and a separate field rather than
	// an overloaded AnsibleVersion for a measured reason: the server screens
	// that field only for pre-releases, so a label would pass it -- and a label
	// named "2.21" would shadow a real ansible-core line with no way to tell.
	//
	// Available labels come from ListAnsibleExecutionEnvironments; naming one
	// the deployment has not configured is refused at the write with a 422.
	AnsibleExecutionEnvironment string `json:"ansible-execution-environment,omitempty"`
	// AnsibleCollections are the collections a configure installs, declared on
	// the platform (#1931). Installed AFTER the configuration's own
	// requirements file and with --force, so an entry here overrides the
	// repository's build of that collection and leaves the rest standing.
	//
	// `!= nil` is how an empty list travels on a write, as with InventoryPaths:
	// `[]` means "declare none here" and has to be distinguishable from "do
	// not touch this".
	AnsibleCollections []AnsibleCollection `json:"ansible-collections,omitempty"`
	ResourceMemory     string              `json:"resource-memory,omitempty"`
	// PlanApplyEnabled and ConfigureEnabled turn an operation on or off
	// (#1966). POINTERS so that nil leaves the stored value alone -- a plain
	// bool would flip the field to false on every PATCH that did not mention
	// it, which is the trap AutoApply's own comment above records.
	//
	// The server checks the both-false invariant against the MERGE of what is
	// sent with what is stored, so turning plan/apply off on a workspace that
	// already has configure on succeeds, and turning it off on one that does
	// not is a 422 rather than an inert workspace.
	PlanApplyEnabled *bool `json:"plan-apply-enabled,omitempty"`
	ConfigureEnabled *bool `json:"configure-enabled,omitempty"`
	// AutoConfigureEnabled and ConfigureAfterApply turn a configure trigger on
	// or off (#1974). POINTERS so that nil leaves the stored value alone.
	AutoConfigureEnabled *bool `json:"auto-configure-enabled,omitempty"`
	ConfigureAfterApply  *bool `json:"configure-after-apply,omitempty"`
	// ConfigureAllHosts turns the narrowing off or back on (#2229). A POINTER
	// so nil leaves the stored value alone.
	ConfigureAllHosts *bool `json:"configure-all-hosts,omitempty"`
	// ConfigDirectory moves where the ansible configure operation is rooted
	// (#1929). Still a POINTER here, and for a different reason from the two
	// fields above: a PATCH needs to distinguish "leave it alone" from "set it
	// to the configuration version's root".
	//
	//	nil               -- leave the stored value alone
	//	&"ansible"        -- root the operation at that subdirectory
	//	&""               -- root it at the configuration version's root
	//
	// There is no fourth state and no Clear flag any more: an explicit null is
	// a 422 from the server, because the field is a path and a path has
	// nothing to be cleared to. Whether the workspace HAS a configure
	// operation is ConfigureEnabled above (#1966).
	ConfigDirectory *string `json:"config-directory,omitempty"`
	// InventoryPaths are the ordered inventory sources. A nil slice is
	// omitted and leaves the stored list alone; an empty non-nil slice
	// (`[]string{}`) is sent as `[]`, which is how every source is removed.
	InventoryPaths []string `json:"inventory-paths,omitempty"`
	// DefaultPlaybook and DefaultLimit are POINTERS so a caller can clear
	// either with &"". A plain string would drop the empty value and "stop
	// defaulting to a playbook" would be a silent no-op -- the same defect
	// `debug-mode` and `ai-policy-mode` shipped with.
	DefaultPlaybook *string `json:"default-playbook,omitempty"`
	DefaultLimit    *string `json:"default-limit,omitempty"`
	// InventoryIncludePlatform is a pointer for the same reason: the default
	// is true, so false has to be able to travel.
	InventoryIncludePlatform *bool `json:"inventory-include-platform,omitempty"`
	// InventoryIgnorePaths excludes paths from the resolution. Same
	// nil-versus-empty rule as InventoryPaths.
	InventoryIgnorePaths []string `json:"inventory-ignore-paths,omitempty"`
	// OIDCAudiences is the per-workspace cloud identity override (#1901), keyed
	// on the provider configuration: `aws`, or `aws.west` for one aliased
	// configuration. It is MERGED over the deployment's catalogue per key, so
	// setting one key does not restate the rest and removing one falls back to
	// the deployment's value.
	//
	// A nil map leaves the server-side value alone; an empty non-nil map
	// (`map[string][]string{}`) clears it, which is how a workspace drops every
	// override and inherits the catalogue wholesale.
	//
	// A key present with an EMPTY list is refused (422). "No audiences for this
	// target" is expressed by removing the key, which falls back to the
	// deployment's configured value — an empty list would otherwise be an
	// opt-out that looks identical to a typo.
	OIDCAudiences                 map[string][]string `json:"oidc-audiences,omitempty"`
	VCSRepoURL                    string              `json:"vcs-repo-url,omitempty"`
	VCSBranch                     string              `json:"vcs-branch,omitempty"`
	VCSWorkflow                   string              `json:"vcs-workflow,omitempty"`
	VCSConnectionID               string              `json:"-"`
	AgentPoolID                   string              `json:"agent-pool-id,omitempty"`
	AgentPoolIDs                  []string            `json:"agent-pool-ids,omitempty"` // whole pool set; mutually exclusive with AgentPoolID (422)
	AutoMerge                     *bool               `json:"auto-merge,omitempty"`
	AutoMergeStrategy             string              `json:"auto-merge-strategy,omitempty"`
	Labels                        map[string]string   `json:"labels,omitempty"`
	VarFiles                      []string            `json:"var-files,omitempty"`
	TriggerPrefixes               []string            `json:"trigger-prefixes,omitempty"`
	DriftIgnoreRules              []string            `json:"drift-ignore-rules,omitempty"`
	DriftDetectionEnabled         *bool               `json:"drift-detection-enabled,omitempty"`
	DriftDetectionIntervalSeconds *int64              `json:"drift-detection-interval-seconds,omitempty"`
	SecurityScanEnforcement       string              `json:"security-scan-enforcement,omitempty"`
	SecurityScanEngine            string              `json:"security-scan-engine,omitempty"`
	SecurityScanSeverityThreshold string              `json:"security-scan-severity-threshold,omitempty"`
	SecurityScanSkipRules         []string            `json:"security-scan-skip-rules,omitempty"`
	PlanExpirySeconds             *int64              `json:"plan-expiry-seconds,omitempty"`
	// AISummaryMode see CreateWorkspaceRequest. On UPDATE, empty string
	// leaves the existing value untouched — to explicitly set "follow
	// deployment default", pass "default".
	// DebugMode holds this workspace's failed runner pods open for inspection
	// (#1764). A pointer so that "leave it alone" and "turn it off" stay
	// distinguishable -- a bare bool with omitempty cannot express the second.
	DebugMode *bool `json:"debug-mode,omitempty"`
	// AllowForkPRPlans permits a speculative plan for a pull request opened
	// from a fork (GHSA-gp5w-76rw-c452). A pointer for the same reason as
	// DebugMode: a bare bool with omitempty cannot distinguish "leave it
	// alone" from "turn it off", and this one gates whether untrusted code
	// sees the credentials.
	AllowForkPRPlans *bool  `json:"allow-fork-pr-plans,omitempty"`
	AISummaryMode    string `json:"ai-summary-mode,omitempty"`
	// AIPolicyMode see CreateWorkspaceRequest. On UPDATE, empty string leaves
	// the existing value untouched -- to explicitly set "follow the deployment
	// default", pass "default".
	AIPolicyMode string `json:"ai-policy-mode,omitempty"`
	// AISummaryContext see CreateWorkspaceRequest. To clear an existing
	// context, set this to "" — but note empty string also means
	// "leave alone" (a Terrapod-side limitation; clear via the UI).
	AISummaryContext *string `json:"ai-summary-context,omitempty"`
	// SlackChannel opts this workspace into Slack run notifications (#556).
	// Pointer-typed so "" (clear/go-silent) is distinguishable from nil
	// (leave alone).
	SlackChannel *string `json:"slack-channel,omitempty"`
}

// WorkspaceListOptions filters and paginates ListWorkspaces. Zero
// values produce a default-paged unfiltered list.
type WorkspaceListOptions struct {
	// PageNumber is 1-indexed (matches the server). 0 means "page 1".
	PageNumber int
	// PageSize defaults to the server's default (20) when 0.
	PageSize int
	// Search matches workspace names with prefix/substring semantics
	// (server-controlled). Empty means no name filter.
	Search string
	// Engine narrows the list to one engine ("terraform", "pulumi"). Empty
	// means every engine the server enables (#1554).
	Engine string
}

// WorkspaceList is the paginated result of ListWorkspaces.
type WorkspaceList struct {
	Items       []Workspace
	CurrentPage int
	PageSize    int
	TotalPages  int
	TotalCount  int
}

// CreateWorkspace creates a new workspace in the default organisation
// (Terrapod is single-org by design). Returns the created Workspace
// with server-populated fields filled in (id, created_at, etc.).
//
// Common errors:
//   - *ConflictError when the name is already taken
//   - *ValidationError on invalid attribute values
//   - *AuthorizationError when the token lacks `admin` on the workspace
func (c *Client) CreateWorkspace(ctx context.Context, req CreateWorkspaceRequest) (*Workspace, error) {
	attrs := workspaceCreateAttrs(req)
	rels := workspaceRels(req.VCSConnectionID)
	body, err := MarshalResource("workspaces", attrs, rels)
	if err != nil {
		return nil, fmt.Errorf("marshal create workspace: %w", err)
	}
	// Only an engine-bearing create needs the native route, and only a server
	// new enough to serve it could honour the field anyway. Keeping the default
	// on the long-standing path means existing callers are byte-identical and
	// still work against a server that predates the native endpoint.
	path := "/api/v2/organizations/default/workspaces"
	if req.Engine != "" {
		path = "/api/v1/workspaces"
	}
	data, err := c.Post(ctx, path, body)
	if err != nil {
		return nil, err
	}
	return parseWorkspace(data)
}

// GetWorkspace reads a workspace by id ("ws-..."). Returns
// *NotFoundError when the id is unknown.
func (c *Client) GetWorkspace(ctx context.Context, id string) (*Workspace, error) {
	data, err := c.nativeFirst(
		func() ([]byte, error) { return c.Get(ctx, "/api/v1/workspaces/"+url.PathEscape(id)) },
		func() ([]byte, error) { return c.Get(ctx, "/api/v2/workspaces/"+url.PathEscape(id)) },
	)
	if err != nil {
		return nil, err
	}
	return parseWorkspace(data)
}

// GetWorkspaceByName reads a workspace by its name (the human-
// recognisable identifier operators use). Useful for import flows
// where the operator types the name they see in the UI rather than
// the typed-id form.
func (c *Client) GetWorkspaceByName(ctx context.Context, name string) (*Workspace, error) {
	data, err := c.nativeFirst(
		func() ([]byte, error) { return c.Get(ctx, "/api/v1/workspaces/"+url.PathEscape(name)) },
		func() ([]byte, error) {
			return c.Get(ctx, "/api/v2/organizations/default/workspaces/"+url.PathEscape(name))
		},
	)
	if err != nil {
		return nil, err
	}
	return parseWorkspace(data)
}

// UpdateWorkspace partially updates the workspace identified by id.
// Fields whose zero value should be a no-op (don't set this field on
// the server) use pointer types; setting a nil pointer leaves the
// server-side value unchanged.
//
// Returns the updated Workspace.
func (c *Client) UpdateWorkspace(ctx context.Context, id string, req UpdateWorkspaceRequest) (*Workspace, error) {
	attrs := workspaceUpdateAttrs(req)
	rels := workspaceRels(req.VCSConnectionID)
	body, err := MarshalResourceWithIDAndRels(id, "workspaces", attrs, rels)
	if err != nil {
		return nil, fmt.Errorf("marshal update workspace: %w", err)
	}
	data, err := c.nativeFirst(
		func() ([]byte, error) { return c.Patch(ctx, "/api/v1/workspaces/"+url.PathEscape(id), body) },
		func() ([]byte, error) { return c.Patch(ctx, "/api/v2/workspaces/"+url.PathEscape(id), body) },
	)
	if err != nil {
		return nil, err
	}
	return parseWorkspace(data)
}

// DeleteWorkspace removes the workspace identified by id. The endpoint
// lives on the Terrapod-native prefix (not /api/v2/) — workspace
// deletion isn't a CLI-consumed surface and the v2 alias was removed
// in #278.
//
// Returns nil on success, *NotFoundError if the workspace doesn't
// exist (treated as success by most callers — idempotent delete).
func (c *Client) DeleteWorkspace(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/terrapod/v1/workspaces/"+url.PathEscape(id))
}

// ListWorkspaces returns one page of workspaces. The caller drives
// pagination explicitly via opts.PageNumber so a large org with
// thousands of workspaces still finishes (the alternative —
// auto-pagination inside this method — would block for minutes).
func (c *Client) ListWorkspaces(ctx context.Context, opts WorkspaceListOptions) (*WorkspaceList, error) {
	q := url.Values{}
	if opts.PageNumber > 0 {
		q.Set("page[number]", strconv.Itoa(opts.PageNumber))
	}
	if opts.PageSize > 0 {
		q.Set("page[size]", strconv.Itoa(opts.PageSize))
	}
	if opts.Search != "" {
		q.Set("search[name]", opts.Search)
	}
	if opts.Engine != "" {
		q.Set("filter[engine]", opts.Engine)
	}
	query := ""
	if encoded := q.Encode(); encoded != "" {
		query = "?" + encoded
	}
	data, err := c.nativeFirst(
		func() ([]byte, error) { return c.Get(ctx, "/api/v1/workspaces"+query) },
		func() ([]byte, error) {
			// The TFE list is Terraform-only, so it cannot answer for another
			// engine; say so rather than return the wrong set.
			if opts.Engine != "" && opts.Engine != "terraform" {
				return nil, fmt.Errorf("this server cannot list %s workspaces: it predates the native workspace list", opts.Engine)
			}
			return c.Get(ctx, "/api/v2/organizations/default/workspaces"+query)
		},
	)
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, err
	}
	list := &WorkspaceList{Items: make([]Workspace, 0, len(resources))}
	for i := range resources {
		ws := workspaceFromResource(&resources[i])
		list.Items = append(list.Items, *ws)
	}
	// Pagination meta is in the `meta` block of the document, decoded
	// separately so the resource-list parse stays clean.
	if meta, err := parseListMeta(data); err == nil {
		list.CurrentPage = meta.CurrentPage
		list.PageSize = meta.PageSize
		list.TotalPages = meta.TotalPages
		list.TotalCount = meta.TotalCount
	}
	return list, nil
}

// ListAllWorkspaces fetches every workspace matching opts, paging through
// the whole collection so the caller doesn't have to. It is the robust way
// to "get all workspaces": it loops on the server's meta.total-pages rather
// than relying on any single-response default, so it stays correct even if
// the server imposes a page size. opts.PageNumber is ignored (paging starts
// at 1); opts.PageSize sets the per-request page (defaults to 100 here to
// keep round-trips down), and opts.Search still filters.
//
// Prefer this over ListWorkspaces when you want the complete set; use
// ListWorkspaces when you genuinely want a single page (e.g. surfacing
// total-count to a user without pulling everything).
func (c *Client) ListAllWorkspaces(ctx context.Context, opts WorkspaceListOptions) ([]Workspace, error) {
	pageSize := opts.PageSize
	if pageSize <= 0 {
		pageSize = 100
	}
	var all []Workspace
	for page := 1; ; page++ {
		list, err := c.ListWorkspaces(ctx, WorkspaceListOptions{
			PageNumber: page,
			PageSize:   pageSize,
			Search:     opts.Search,
			Engine:     opts.Engine,
		})
		if err != nil {
			return nil, err
		}
		all = append(all, list.Items...)
		// Stop when we've collected every page. TotalPages is authoritative;
		// fall back to "a short page means the end" if the server omitted meta
		// (older server, or a non-paginating collection).
		if list.TotalPages > 0 {
			if page >= list.TotalPages {
				break
			}
		} else if len(list.Items) < pageSize {
			break
		}
	}
	return all, nil
}

// ── Internal helpers ─────────────────────────────────────────────────

// workspaceCreateAttrs turns a CreateWorkspaceRequest into the JSON:API
// attributes map. Only set keys go in — Terrapod doesn't tolerate
// `null` for fields it expects to omit, and `omitempty` on struct
// tags doesn't help with nested map marshaling.
func workspaceCreateAttrs(req CreateWorkspaceRequest) map[string]any {
	attrs := map[string]any{"name": req.Name}
	if req.Engine != "" {
		attrs["engine"] = req.Engine
	}
	if req.ExecutionMode != "" {
		attrs["execution-mode"] = req.ExecutionMode
	}
	if req.ExecutionBackend != "" {
		attrs["execution-backend"] = req.ExecutionBackend
	}
	if req.AutoApply != nil {
		attrs["auto-apply"] = *req.AutoApply
	}
	if req.PulumiBindPlan != nil {
		attrs["pulumi-bind-plan"] = *req.PulumiBindPlan
	}
	if req.AutoApplyMode != nil {
		attrs["auto-apply-mode"] = *req.AutoApplyMode
	}
	// Sent under the canonical key only, whichever field carried it: two keys
	// could disagree, and the server refuses a request that does (#1559).
	if req.EngineVersion != "" {
		attrs["engine-version"] = req.EngineVersion
	} else if req.TerraformVersion != "" {
		attrs["engine-version"] = req.TerraformVersion
	}
	if req.TerragruntEnabled != nil {
		attrs["terragrunt-enabled"] = *req.TerragruntEnabled
	}
	if req.TerragruntVersion != "" {
		attrs["terragrunt-version"] = req.TerragruntVersion
	}
	if req.WorkingDirectory != "" {
		attrs["working-directory"] = req.WorkingDirectory
	}
	if req.AnsibleVersion != "" {
		attrs["ansible-version"] = req.AnsibleVersion
	}
	if req.AnsibleExecutionEnvironment != "" {
		attrs["ansible-execution-environment"] = req.AnsibleExecutionEnvironment
	}
	// #1929's six. `config-directory` is the only one whose null carries a
	// meaning, so it is the only one guarded on a pointer rather than on an
	// empty value: a non-nil pointer travels even when it points at "",
	// because "" is the repository root and nil is "no configure operation".
	if req.PlanApplyEnabled != nil {
		attrs["plan-apply-enabled"] = *req.PlanApplyEnabled
	}
	if req.ConfigureEnabled != nil {
		attrs["configure-enabled"] = *req.ConfigureEnabled
	}
	if req.AutoConfigureEnabled != nil {
		attrs["auto-configure-enabled"] = *req.AutoConfigureEnabled
	}
	if req.ConfigureAfterApply != nil {
		attrs["configure-after-apply"] = *req.ConfigureAfterApply
	}
	if req.ConfigureAllHosts != nil {
		attrs["configure-all-hosts"] = *req.ConfigureAllHosts
	}
	if req.ConfigDirectory != "" {
		attrs["config-directory"] = req.ConfigDirectory
	}
	if req.InventoryPaths != nil {
		// `!= nil` not `len() > 0`: an explicit empty slice is a real value,
		// so it has to travel.
		attrs["inventory-paths"] = req.InventoryPaths
	}
	if req.AnsibleCollections != nil {
		attrs["ansible-collections"] = req.AnsibleCollections
	}
	if req.DefaultPlaybook != "" {
		attrs["default-playbook"] = req.DefaultPlaybook
	}
	if req.DefaultLimit != "" {
		attrs["default-limit"] = req.DefaultLimit
	}
	if req.InventoryIncludePlatform != nil {
		attrs["inventory-include-platform"] = *req.InventoryIncludePlatform
	}
	if req.InventoryIgnorePaths != nil {
		attrs["inventory-ignore-paths"] = req.InventoryIgnorePaths
	}
	if req.Parallelism != 0 {
		attrs["parallelism"] = req.Parallelism
	}
	if req.EEResourceCPU != "" {
		attrs["ee-resource-cpu"] = req.EEResourceCPU
	}
	if req.EEResourceMemory != "" {
		attrs["ee-resource-memory"] = req.EEResourceMemory
	}
	if req.ResourceCPU != "" {
		attrs["resource-cpu"] = req.ResourceCPU
	}
	if req.ResourceMemory != "" {
		attrs["resource-memory"] = req.ResourceMemory
	}
	if req.VCSRepoURL != "" {
		attrs["vcs-repo-url"] = req.VCSRepoURL
	}
	if req.VCSBranch != "" {
		attrs["vcs-branch"] = req.VCSBranch
	}
	if req.VCSWorkflow != "" {
		attrs["vcs-workflow"] = req.VCSWorkflow
	}
	if req.AgentPoolID != "" {
		attrs["agent-pool-id"] = req.AgentPoolID
	}
	if req.AgentPoolIDs != nil {
		attrs["agent-pool-ids"] = req.AgentPoolIDs
	}
	if req.AutoMerge != nil {
		attrs["auto-merge"] = *req.AutoMerge
	}
	if req.AutoMergeStrategy != "" {
		attrs["auto-merge-strategy"] = req.AutoMergeStrategy
	}
	if req.OwnerEmail != "" {
		attrs["owner-email"] = req.OwnerEmail
	}
	if req.Labels != nil {
		attrs["labels"] = req.Labels
	}
	if req.OIDCAudiences != nil {
		// `!= nil` not `len() > 0`: an explicit empty map is how a workspace
		// drops every override, so it has to travel. Omitting it would leave
		// the server-side value alone and the opt-out would silently not apply.
		attrs["oidc-audiences"] = req.OIDCAudiences
	}
	if req.VarFiles != nil {
		attrs["var-files"] = req.VarFiles
	}
	if req.TriggerPrefixes != nil {
		attrs["trigger-prefixes"] = req.TriggerPrefixes
	}
	if req.DriftIgnoreRules != nil {
		attrs["drift-ignore-rules"] = req.DriftIgnoreRules
	}
	if req.DriftDetectionEnabled != nil {
		attrs["drift-detection-enabled"] = *req.DriftDetectionEnabled
	}
	if req.DriftDetectionIntervalSeconds != nil {
		attrs["drift-detection-interval-seconds"] = *req.DriftDetectionIntervalSeconds
	}
	if req.SecurityScanEnforcement != "" {
		attrs["security-scan-enforcement"] = req.SecurityScanEnforcement
	}
	if req.SecurityScanEngine != "" {
		attrs["security-scan-engine"] = req.SecurityScanEngine
	}
	if req.SecurityScanSeverityThreshold != "" {
		attrs["security-scan-severity-threshold"] = req.SecurityScanSeverityThreshold
	}
	if req.SecurityScanSkipRules != nil {
		attrs["security-scan-skip-rules"] = req.SecurityScanSkipRules
	}
	if req.PlanExpirySeconds != nil {
		attrs["plan-expiry-seconds"] = *req.PlanExpirySeconds
	}
	if req.AISummaryMode != "" {
		attrs["ai-summary-mode"] = req.AISummaryMode
	}
	if req.AISummaryContext != "" {
		attrs["ai-summary-context"] = req.AISummaryContext
	}
	if req.AIPolicyMode != "" {
		attrs["ai-policy-mode"] = req.AIPolicyMode
	}
	if req.DebugMode != nil {
		attrs["debug-mode"] = *req.DebugMode
	}
	if req.AllowForkPRPlans != nil {
		attrs["allow-fork-pr-plans"] = *req.AllowForkPRPlans
	}
	if req.SlackChannel != "" {
		attrs["slack-channel"] = req.SlackChannel
	}
	return attrs
}

// workspaceUpdateAttrs mirrors workspaceCreateAttrs minus OwnerEmail
// (which only platform admins can change, via a separate endpoint).
// Name is included so PATCH can rename a workspace; the empty-string
// check on every field is what makes the "leave alone" semantics work
// on every PATCH.
func workspaceUpdateAttrs(req UpdateWorkspaceRequest) map[string]any {
	attrs := map[string]any{}
	if req.Name != "" {
		attrs["name"] = req.Name
	}
	if req.ExecutionMode != "" {
		attrs["execution-mode"] = req.ExecutionMode
	}
	if req.ExecutionBackend != "" {
		attrs["execution-backend"] = req.ExecutionBackend
	}
	if req.AutoApply != nil {
		attrs["auto-apply"] = *req.AutoApply
	}
	if req.PulumiBindPlan != nil {
		attrs["pulumi-bind-plan"] = *req.PulumiBindPlan
	}
	if req.AutoApplyMode != nil {
		attrs["auto-apply-mode"] = *req.AutoApplyMode
	}
	// Sent under the canonical key only, whichever field carried it: two keys
	// could disagree, and the server refuses a request that does (#1559).
	if req.EngineVersion != "" {
		attrs["engine-version"] = req.EngineVersion
	} else if req.TerraformVersion != "" {
		attrs["engine-version"] = req.TerraformVersion
	}
	if req.TerragruntEnabled != nil {
		attrs["terragrunt-enabled"] = *req.TerragruntEnabled
	}
	if req.TerragruntVersion != "" {
		attrs["terragrunt-version"] = req.TerragruntVersion
	}
	if req.WorkingDirectory != "" {
		attrs["working-directory"] = req.WorkingDirectory
	}
	if req.AnsibleVersion != "" {
		attrs["ansible-version"] = req.AnsibleVersion
	}
	if req.AnsibleExecutionEnvironment != "" {
		attrs["ansible-execution-environment"] = req.AnsibleExecutionEnvironment
	}
	// #1966's two, then #1929's six. Each is sent only when the caller set it,
	// so a PATCH that mentions none of them leaves all eight alone.
	if req.PlanApplyEnabled != nil {
		attrs["plan-apply-enabled"] = *req.PlanApplyEnabled
	}
	if req.ConfigureEnabled != nil {
		attrs["configure-enabled"] = *req.ConfigureEnabled
	}
	if req.AutoConfigureEnabled != nil {
		attrs["auto-configure-enabled"] = *req.AutoConfigureEnabled
	}
	if req.ConfigureAfterApply != nil {
		attrs["configure-after-apply"] = *req.ConfigureAfterApply
	}
	if req.ConfigureAllHosts != nil {
		attrs["configure-all-hosts"] = *req.ConfigureAllHosts
	}
	// A pointer so that `&""` can root the operation at the configuration
	// version's root while nil leaves the stored value alone. There is no null
	// case: the server refuses one with a 422 (#1966).
	if req.ConfigDirectory != nil {
		attrs["config-directory"] = *req.ConfigDirectory
	}
	if req.InventoryPaths != nil {
		// `!= nil` not `len() > 0`: an explicit empty slice is how every
		// source is removed, so it has to travel as `[]`.
		attrs["inventory-paths"] = req.InventoryPaths
	}
	if req.AnsibleCollections != nil {
		// Same rule: `[]` is how every declared collection is removed.
		attrs["ansible-collections"] = req.AnsibleCollections
	}
	if req.DefaultPlaybook != nil {
		// *string so a caller can explicitly stop defaulting to a playbook
		// with &"".
		attrs["default-playbook"] = *req.DefaultPlaybook
	}
	if req.DefaultLimit != nil {
		// *string so &"" can widen a configure run back to the whole
		// inventory.
		attrs["default-limit"] = *req.DefaultLimit
	}
	if req.InventoryIncludePlatform != nil {
		attrs["inventory-include-platform"] = *req.InventoryIncludePlatform
	}
	if req.InventoryIgnorePaths != nil {
		attrs["inventory-ignore-paths"] = req.InventoryIgnorePaths
	}
	if req.Parallelism != 0 {
		attrs["parallelism"] = req.Parallelism
	}
	if req.EEResourceCPU != "" {
		attrs["ee-resource-cpu"] = req.EEResourceCPU
	}
	if req.EEResourceMemory != "" {
		attrs["ee-resource-memory"] = req.EEResourceMemory
	}
	if req.ResourceCPU != "" {
		attrs["resource-cpu"] = req.ResourceCPU
	}
	if req.ResourceMemory != "" {
		attrs["resource-memory"] = req.ResourceMemory
	}
	if req.VCSRepoURL != "" {
		attrs["vcs-repo-url"] = req.VCSRepoURL
	}
	if req.VCSBranch != "" {
		attrs["vcs-branch"] = req.VCSBranch
	}
	if req.VCSWorkflow != "" {
		attrs["vcs-workflow"] = req.VCSWorkflow
	}
	if req.AgentPoolID != "" {
		attrs["agent-pool-id"] = req.AgentPoolID
	}
	if req.AgentPoolIDs != nil {
		attrs["agent-pool-ids"] = req.AgentPoolIDs
	}
	if req.AutoMerge != nil {
		attrs["auto-merge"] = *req.AutoMerge
	}
	if req.AutoMergeStrategy != "" {
		attrs["auto-merge-strategy"] = req.AutoMergeStrategy
	}
	if req.Labels != nil {
		attrs["labels"] = req.Labels
	}
	if req.OIDCAudiences != nil {
		// `!= nil` not `len() > 0`: an explicit empty map is how a workspace
		// drops every override, so it has to travel. Omitting it would leave
		// the server-side value alone and the opt-out would silently not apply.
		attrs["oidc-audiences"] = req.OIDCAudiences
	}
	if req.VarFiles != nil {
		attrs["var-files"] = req.VarFiles
	}
	if req.TriggerPrefixes != nil {
		attrs["trigger-prefixes"] = req.TriggerPrefixes
	}
	if req.DriftIgnoreRules != nil {
		attrs["drift-ignore-rules"] = req.DriftIgnoreRules
	}
	if req.DriftDetectionEnabled != nil {
		attrs["drift-detection-enabled"] = *req.DriftDetectionEnabled
	}
	if req.DriftDetectionIntervalSeconds != nil {
		attrs["drift-detection-interval-seconds"] = *req.DriftDetectionIntervalSeconds
	}
	if req.SecurityScanEnforcement != "" {
		attrs["security-scan-enforcement"] = req.SecurityScanEnforcement
	}
	if req.SecurityScanEngine != "" {
		attrs["security-scan-engine"] = req.SecurityScanEngine
	}
	if req.SecurityScanSeverityThreshold != "" {
		attrs["security-scan-severity-threshold"] = req.SecurityScanSeverityThreshold
	}
	if req.SecurityScanSkipRules != nil {
		attrs["security-scan-skip-rules"] = req.SecurityScanSkipRules
	}
	if req.PlanExpirySeconds != nil {
		attrs["plan-expiry-seconds"] = *req.PlanExpirySeconds
	}
	if req.AISummaryMode != "" {
		attrs["ai-summary-mode"] = req.AISummaryMode
	}
	if req.AISummaryContext != nil {
		// *string so callers can explicitly clear the context with &"".
		attrs["ai-summary-context"] = *req.AISummaryContext
	}
	if req.AIPolicyMode != "" {
		attrs["ai-policy-mode"] = req.AIPolicyMode
	}
	if req.DebugMode != nil {
		attrs["debug-mode"] = *req.DebugMode
	}
	if req.AllowForkPRPlans != nil {
		attrs["allow-fork-pr-plans"] = *req.AllowForkPRPlans
	}
	if req.SlackChannel != nil {
		// *string so callers can explicitly go silent with &"".
		attrs["slack-channel"] = *req.SlackChannel
	}
	return attrs
}

// workspaceRels builds the relationships block for create + update.
// Empty vcsConnectionID returns nil — caller's MarshalResource handles
// that by omitting the `relationships` key entirely.
func workspaceRels(vcsConnectionID string) map[string]any {
	if vcsConnectionID == "" {
		return nil
	}
	return map[string]any{
		"vcs-connection": map[string]any{
			"data": map[string]any{
				"id":   vcsConnectionID,
				"type": "vcs-connections",
			},
		},
	}
}

// parseWorkspace decodes a JSON:API single-workspace response.
func parseWorkspace(body []byte) (*Workspace, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse workspace response: %w", err)
	}
	return workspaceFromResource(res), nil
}

// workspaceFromResource projects the raw Resource into a typed Workspace.
// Centralised so the list-decoding path uses the same projection as
// the single-resource path.
// engineVersionAttr reads the engine version under whichever name the server
// sent (#1559).
//
// A current server sends both, always equal, so the canonical key answers.
// Falling back to "terraform-version" is what lets this SDK keep working
// against a server from before the rename, where only that key exists.
func engineVersionAttr(res *Resource) string {
	if v := GetStringAttr(res, "engine-version"); v != "" {
		return v
	}
	return GetStringAttr(res, "terraform-version")
}

func workspaceFromResource(res *Resource) *Workspace {
	version := engineVersionAttr(res)
	ws := &Workspace{
		ID:               res.ID,
		Name:             GetStringAttr(res, "name"),
		ExecutionMode:    GetStringAttr(res, "execution-mode"),
		Engine:           GetStringAttr(res, "engine"),
		PulumiBindPlan:   GetBoolAttr(res, "pulumi-bind-plan"),
		ExecutionBackend: GetStringAttr(res, "execution-backend"),
		AutoApply:        GetBoolAttr(res, "auto-apply"),
		AutoApplyMode:    GetStringAttr(res, "auto-apply-mode"),
		// Both fields carry the resolved value, so a caller reading either one
		// gets the right answer whichever name the server used (#1559).
		EngineVersion:               version,
		TerraformVersion:            version,
		TerragruntEnabled:           GetBoolAttr(res, "terragrunt-enabled"),
		TerragruntVersion:           GetStringAttr(res, "terragrunt-version"),
		WorkingDirectory:            GetStringAttr(res, "working-directory"),
		ResourceCPU:                 GetStringAttr(res, "resource-cpu"),
		EEResourceCPU:               GetStringAttr(res, "ee-resource-cpu"),
		EEResourceMemory:            GetStringAttr(res, "ee-resource-memory"),
		Parallelism:                 GetIntAttr(res, "parallelism"),
		AnsibleVersion:              GetStringAttr(res, "ansible-version"),
		AnsibleExecutionEnvironment: GetStringAttr(res, "ansible-execution-environment"),
		ResourceMemory:              GetStringAttr(res, "resource-memory"),
		// #1929's six. `config-directory` reads through GetStringPtrAttr so a
		// JSON null lands as nil -- the absent state -- rather than as "",
		// which on this field means the repository root. GetStringAttr would
		// collapse the two and a consumer writing a read back would root a
		// configure operation at the top of every repository.
		PlanApplyEnabled:              GetBoolAttr(res, "plan-apply-enabled"),
		ConfigureEnabled:              GetBoolAttr(res, "configure-enabled"),
		AutoConfigureEnabled:          GetBoolAttr(res, "auto-configure-enabled"),
		ConfigureAfterApply:           GetBoolAttr(res, "configure-after-apply"),
		ConfigureAllHosts:             GetBoolAttr(res, "configure-all-hosts"),
		ConfigDirectory:               GetStringAttr(res, "config-directory"),
		InventoryPaths:                GetListAttr(res, "inventory-paths"),
		AnsibleCollections:            GetAnsibleCollectionsAttr(res, "ansible-collections"),
		DefaultPlaybook:               GetStringAttr(res, "default-playbook"),
		DefaultLimit:                  GetStringAttr(res, "default-limit"),
		InventoryIgnorePaths:          GetListAttr(res, "inventory-ignore-paths"),
		VCSRepoURL:                    GetStringAttr(res, "vcs-repo-url"),
		VCSBranch:                     GetStringAttr(res, "vcs-branch"),
		VCSWorkflow:                   GetStringAttr(res, "vcs-workflow"),
		VCSConnectionID:               GetRelationshipID(res, "vcs-connection"),
		AgentPoolID:                   GetStringAttr(res, "agent-pool-id"),
		AgentPoolIDs:                  GetListAttr(res, "agent-pool-ids"),
		AgentPoolNames:                GetListAttr(res, "agent-pool-names"),
		AutoMerge:                     GetBoolAttr(res, "auto-merge"),
		AutoMergeStrategy:             GetStringAttr(res, "auto-merge-strategy"),
		OwnerEmail:                    GetStringAttr(res, "owner-email"),
		Locked:                        GetBoolAttr(res, "locked"),
		LockReason:                    GetStringAttr(res, "lock-reason"),
		LockedBy:                      GetStringAttr(res, "locked-by"),
		Labels:                        GetMapAttr(res, "labels"),
		OIDCAudiences:                 GetAudienceMapAttr(res, "oidc-audiences"),
		VarFiles:                      GetListAttr(res, "var-files"),
		TriggerPrefixes:               GetListAttr(res, "trigger-prefixes"),
		DriftIgnoreRules:              GetListAttr(res, "drift-ignore-rules"),
		DriftDetectionEnabled:         GetBoolAttr(res, "drift-detection-enabled"),
		SecurityScanEnforcement:       GetStringAttr(res, "security-scan-enforcement"),
		SecurityScanEngine:            GetStringAttr(res, "security-scan-engine"),
		SecurityScanSeverityThreshold: GetStringAttr(res, "security-scan-severity-threshold"),
		SecurityScanSkipRules:         GetListAttr(res, "security-scan-skip-rules"),
		DriftStatus:                   GetStringAttr(res, "drift-status"),
		DriftLastCheckedAt:            GetStringAttr(res, "drift-last-checked-at"),
		DriftLatestRunID:              GetStringAttr(res, "drift-latest-run-id"),
		StateDiverged:                 GetBoolAttr(res, "state-diverged"),
		LifecycleState:                GetStringAttr(res, "lifecycle-state"),
		LifecycleReason:               GetStringAttr(res, "lifecycle-reason"),
		VCSLastPolledAt:               GetStringAttr(res, "vcs-last-polled-at"),
		VCSLastAttemptedAt:            GetStringAttr(res, "vcs-last-attempted-at"),
		VCSLastError:                  GetStringAttr(res, "vcs-last-error"),
		VCSLastErrorAt:                GetStringAttr(res, "vcs-last-error-at"),
		AgentPoolName:                 GetStringAttr(res, "agent-pool-name"),
		VCSConnectionName:             GetStringAttr(res, "vcs-connection-name"),
		DebugMode:                     GetBoolAttr(res, "debug-mode"),
		AllowForkPRPlans:              GetBoolAttr(res, "allow-fork-pr-plans"),
		AISummaryMode:                 GetStringAttr(res, "ai-summary-mode"),
		AISummaryContext:              GetStringAttr(res, "ai-summary-context"),
		AIPolicyMode:                  GetStringAttr(res, "ai-policy-mode"),
		SlackChannel:                  GetStringAttr(res, "slack-channel"),
		CreatedAt:                     GetStringAttr(res, "created-at"),
		UpdatedAt:                     GetStringAttr(res, "updated-at"),
	}
	// `inventory-include-platform` defaults to TRUE, so an absent key is not
	// false -- it is a server that predates #1929, or the TFE-compatible route,
	// which drops all six. Reading it as false would report the declared rows
	// as excluded on every workspace of every older server, and a provider
	// would then show drift on a setting nobody had touched.
	ws.InventoryIncludePlatform = true
	if v := GetBoolPtrAttr(res, "inventory-include-platform"); v != nil {
		ws.InventoryIncludePlatform = *v
	}
	if v := GetIntAttr(res, "drift-detection-interval-seconds"); v > 0 {
		ws.DriftDetectionIntervalSeconds = &v
	}
	// Written by BOTH request builders and served by the API, but never read
	// back until now -- so it was `nil` on every read, the provider's
	// non-nil guard always took the null branch, and
	// `plan_expiry_seconds = 3600` failed the apply with "Provider produced
	// inconsistent result". The same defect as `debug-mode`, in the same
	// struct, three lines from the pattern that fixes it.
	if v := GetIntAttr(res, "plan-expiry-seconds"); v > 0 {
		ws.PlanExpirySeconds = &v
	}
	return ws
}

// nativeFirst reads or writes a workspace through the native route, and falls
// back to the TFE-compatible one only when the server predates it (#1554).
//
// The TFE surface serves Terraform workspaces alone, so the native route is the
// one that reaches every engine. An older server answers the native path with
// 405, because the path exists there for another method; that status, and only
// that one, means "not here yet". Any other answer, a 404 included, is the real
// answer and is returned as is.
func (c *Client) nativeFirst(native, legacy func() ([]byte, error)) ([]byte, error) {
	data, err := native()
	var apiErr *APIError
	if errors.As(err, &apiErr) && apiErr.StatusCode == http.StatusMethodNotAllowed {
		return legacy()
	}
	return data, err
}
