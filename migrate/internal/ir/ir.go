// Package ir defines the intermediate representation that source plugins
// produce and the Terrapod writer consumes.
//
// The IR is the single contract that keeps sources and writer decoupled:
// a source plugin (atlantis, tfe) only knows how to produce Plan and the
// items inside it; the writer only knows how to translate Plan items
// into Terrapod API calls. Adding a third source later (Digger,
// Terrateam) is one new directory under internal/sources/ — no writer
// changes required.
//
// Every item carries SourceID (an opaque string the source uses to
// identify the upstream object — the TFE workspace UUID, or the
// "<repo>/<dir>" path for an atlantis.yaml project) so the migration
// state file can map SourceID → TerrapodID for idempotency. SourceID is
// guaranteed unique within a single Plan from a single source.
//
// Fields are intentionally a superset of what any one source emits:
// missing fields on the source side stay zero, the writer ignores
// zero-valued fields, and we don't have to thread a feature matrix
// through the writer per source. Adding a new field is back-compatible.
//
// JSON tags on every field — the IR is dumped to stdout in dry-run mode
// and serialised to disk in the migration state file. Deterministic
// representation (sorted keys, no embedded time-of-day where avoidable)
// keeps diffs stable across runs.
package ir

// Plan is the top-level IR document. Every source plugin produces one
// per migration; the writer consumes it once.
type Plan struct {
	// Source names the producing plugin: "tfe" | "atlantis". The writer
	// uses this only to populate the `terrapod-migrated-from:{source}`
	// label on every created resource — never to branch on behaviour.
	Source string `json:"source"`

	// SourceMetadata is free-form context the source attaches for
	// operator-facing reports: org name, host URL, repo list, fetch
	// timestamp, source-side version. Never read by the writer.
	SourceMetadata map[string]string `json:"source_metadata,omitempty"`

	// Workspaces is the headline collection. Order is unspecified; the
	// writer may parallelise or sequence as it likes (sequential is the
	// first-release default).
	Workspaces []Workspace `json:"workspaces,omitempty"`

	// VCSConnections to create on the Terrapod side before workspaces
	// that reference them. The writer resolves the dependency.
	VCSConnections []VCSConnection `json:"vcs_connections,omitempty"`

	// VariableSets are org-scoped variable collections applied to many
	// workspaces. Created by the writer AFTER workspaces, so their
	// per-workspace assignments can resolve source workspace IDs to the
	// Terrapod IDs the workspace loop recorded.
	VariableSets []VariableSet `json:"variable_sets,omitempty"`

	// RunTriggers are cross-workspace dependencies (a source workspace's
	// apply queues a run on the destination). Created after workspaces so
	// both endpoints' Terrapod IDs are known; a trigger is created only
	// when BOTH endpoints were migrated.
	RunTriggers []RunTrigger `json:"run_triggers,omitempty"`

	// Notifications are per-workspace notification configurations
	// (generic webhook / Slack / email). Created after workspaces on the
	// migrated destination workspace.
	Notifications []NotificationConfiguration `json:"notifications,omitempty"`

	// AgentPools are named groups of runner listeners. Created after
	// workspaces so their workspace assignments resolve to Terrapod IDs;
	// each pool re-points its migrated member workspaces at the new pool.
	// Join tokens are never portable — every migrated pool needs a fresh
	// token + redeployed listeners, reported for operator follow-up.
	AgentPools []AgentPool `json:"agent_pools,omitempty"`

	// GPGKeys are private-registry provider signing PUBLIC keys. Only the
	// public key is portable (it's not secret); the private key never
	// leaves the operator, so provider *versions* still re-publish via
	// terrapod-publish — but registering the public key up front means the
	// operator doesn't have to re-import it by hand.
	GPGKeys []GPGKey `json:"gpg_keys,omitempty"`

	// Skipped collects items the source decided not to migrate, with a
	// per-item reason. Surfaced in the dry-run report and the handover
	// document; never written to Terrapod.
	Skipped []SkippedItem `json:"skipped,omitempty"`

	// Subsequent increments add: RunTriggers, Notifications, AgentPools,
	// RegistryModules, RegistryProviders, GPGKeys, RoleProposals. Keeping
	// the Plan struct narrow so we don't fix shape before the sources can
	// speak.
}

// Workspace is the migrated form of a TFE workspace or an Atlantis
// project. Field names match Terrapod's create-workspace JSON:API
// attributes where possible to keep the writer's mapping mechanical.
type Workspace struct {
	SourceID         string            `json:"source_id"`
	Name             string            `json:"name"`
	ExecutionMode    string            `json:"execution_mode,omitempty"`    // "local" | "agent"
	TerraformVersion string            `json:"terraform_version,omitempty"` // "1.12", "1.12.3", etc — Terrapod accepts partials
	WorkingDirectory string            `json:"working_directory,omitempty"` // relative path within the repo
	Labels           map[string]string `json:"labels,omitempty"`            // TFE tags translate here: "k:v" → {k: "v"}, "k" → {k: ""}
	AutoApply        bool              `json:"auto_apply,omitempty"`
	OwnerEmail       string            `json:"owner_email,omitempty"`        // set on creation; defaults to migrating user
	VCSConnectionRef string            `json:"vcs_connection_ref,omitempty"` // references VCSConnection.SourceID
	VCSRepoURL       string            `json:"vcs_repo_url,omitempty"`
	VCSBranch        string            `json:"vcs_branch,omitempty"`
	Variables        []Variable        `json:"variables,omitempty"`
	// State and ConfigurationVersion stay external to the Workspace struct
	// because (a) they're large blobs we don't want serialised into the
	// state file and (b) they may need their own retry/streaming policy.
	// The writer pulls them via dedicated source-plugin calls keyed by
	// SourceID.
}

// Variable mirrors a TFE workspace or varset variable. `Value` is
// omitted from JSON when sensitive — sensitive values are read from the
// source by the writer at apply time, never serialised into the state
// file or dry-run report.
type Variable struct {
	Key         string `json:"key"`
	Value       string `json:"value,omitempty"`
	Category    string `json:"category"`      // "terraform" | "env"
	HCL         bool   `json:"hcl,omitempty"` // only meaningful for category=terraform
	Sensitive   bool   `json:"sensitive,omitempty"`
	Description string `json:"description,omitempty"`
}

// VariableSet is the migrated form of a TFE variable set — an
// org-scoped collection of variables applied to many workspaces.
// Variables reuse the Variable shape (sensitive values are read at
// apply time, never serialised, same as workspace variables).
type VariableSet struct {
	SourceID    string `json:"source_id"`
	Name        string `json:"name"`
	Description string `json:"description,omitempty"`
	// Global sets apply to every workspace and carry no explicit
	// WorkspaceRefs. Priority sets override workspace-local variables.
	Global    bool       `json:"global,omitempty"`
	Priority  bool       `json:"priority,omitempty"`
	Variables []Variable `json:"variables,omitempty"`
	// WorkspaceRefs are the SOURCE workspace IDs this set is assigned to
	// (empty when Global). The writer maps each to the Terrapod
	// workspace ID recorded during the workspace loop; refs outside the
	// migration scope are reported as an unresolved assignment.
	WorkspaceRefs []string `json:"workspace_refs,omitempty"`
}

// RunTrigger is a cross-workspace dependency: when the source workspace
// completes an apply, a run is queued on the destination. Both refs are
// SOURCE workspace IDs the writer resolves to Terrapod workspace IDs
// after the workspace loop; the trigger is created only when both
// endpoints were migrated (a ref outside the migration scope is
// reported for manual follow-up). Names are carried for readable reports.
type RunTrigger struct {
	SourceWorkspaceRef      string `json:"source_workspace_ref"`
	DestinationWorkspaceRef string `json:"destination_workspace_ref"`
	SourceName              string `json:"source_name,omitempty"`
	DestinationName         string `json:"destination_name,omitempty"`
}

// NotificationConfiguration is the migrated form of a TFE workspace
// notification configuration. WorkspaceRef is the destination workspace's
// source ID (resolved to a Terrapod workspace id by the writer). Triggers
// are already mapped to Terrapod's trigger vocabulary by the source. The
// HMAC token is write-only at the source (never returned) — for generic
// webhooks that need one, NeedsToken flags it so the operator re-enters
// it post-migration; the config is created with an empty token.
type NotificationConfiguration struct {
	WorkspaceRef    string   `json:"workspace_ref"`
	Name            string   `json:"name"`
	DestinationType string   `json:"destination_type"` // "generic" | "slack" | "email"
	URL             string   `json:"url,omitempty"`
	Enabled         bool     `json:"enabled,omitempty"`
	Triggers        []string `json:"triggers,omitempty"`
	EmailAddresses  []string `json:"email_addresses,omitempty"`
	NeedsToken      bool     `json:"needs_token,omitempty"`
	WorkspaceName   string   `json:"workspace_name,omitempty"`
}

// AgentPool is the migrated form of a TFE agent pool — a named group of
// runner listeners. WorkspaceRefs are the SOURCE workspace IDs assigned
// to the pool (execution-mode=agent); the writer maps each to the
// migrated Terrapod workspace and re-points it at the new pool. Refs
// outside the migration scope are reported for manual follow-up.
//
// Only the pool's identity migrates — TFE agent tokens are write-only
// and never returned, so no token is created. Every migrated pool needs
// a fresh join token and redeployed listeners, which the report flags.
type AgentPool struct {
	SourceID      string   `json:"source_id"`
	Name          string   `json:"name"`
	WorkspaceRefs []string `json:"workspace_refs,omitempty"`
}

// GPGKey is a private-registry provider signing public key. SourceID is
// the upstream key's ID (for idempotent resume); ASCIIArmor is the
// armored PUBLIC key (safe to carry — it is not secret). KeyID is the
// PGP key id, carried for readable reports.
type GPGKey struct {
	SourceID   string `json:"source_id"`
	ASCIIArmor string `json:"ascii_armor"`
	KeyID      string `json:"key_id,omitempty"`
}

// VCSConnection is a Terrapod-side VCS connection (one per source
// OAuth/PAT). On Atlantis migrations there's typically one shared
// connection; on TFE migrations there's one per TFE oauth-client.
type VCSConnection struct {
	SourceID  string `json:"source_id"`
	Name      string `json:"name"`
	Provider  string `json:"provider"`             // "github" | "gitlab"
	ServerURL string `json:"server_url,omitempty"` // empty = provider default (github.com / gitlab.com)
	// Credentials NEVER appear in the IR. The source plugin holds them
	// in memory only and hands them to the writer's CreateVCSConnection
	// call directly. Even the migration state file omits them — only
	// the resulting Terrapod connection id is recorded.
}

// SkippedItem records something the source decided not to migrate. The
// surface is intentionally simple — operators read this list to know
// what they have to do by hand.
type SkippedItem struct {
	// Kind names the resource type as the operator would search for it:
	// "sentinel-policy", "tfe-stack", "bitbucket-vcs-connection",
	// "atlantis-workflow", "atlantis-pre-workflow-hook" etc. Kept as a
	// string (not a typed enum) because the set grows per source.
	Kind string `json:"kind"`

	// Name is the human-recognisable identifier at the source. For a
	// Sentinel policy that's its name; for a VCS connection it's
	// the repo URL or oauth-client name.
	Name string `json:"name"`

	// Reason is operator-readable. Phrase as "<this kind> <name> <why
	// it was skipped>" so the report reads cleanly: "Sentinel policy
	// 'prod-only-no-public-buckets' skipped: not supported by Terrapod
	// (see docs/migration.md#sentinel)".
	Reason string `json:"reason"`
}
