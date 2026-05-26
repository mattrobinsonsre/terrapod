// Package framework holds the source-agnostic orchestration: the
// migration state file, the dry-run/apply runner, scoping filters, the
// RBAC review gate, and the verification driver. Sources and the writer
// import this package; this package never imports sources or writer
// directly (it works through interfaces defined in their respective
// packages so the dependency arrows stay one-way).
//
// Migration state file
// ====================
//
// Every `apply` run reads and writes a JSON file (default
// ./migration-state.json; override with --state-file). The file is:
//
//   * the idempotency record: SourceID → TerrapodID for every created
//     resource, so re-running `apply` after a partial migration is safe
//     and resumes where it left off;
//   * the input the `rewrite` subcommand consumes (Mode 1) to derive
//     source/destination hostnames and the set of workspace names to
//     rewrite — operators don't have to remember those flags after
//     `apply` runs.
//
// Format choices:
//
//   * JSON, not YAML. Deterministic representation (sorted keys, fixed
//     indentation, no trailing whitespace) makes diffs between
//     successive runs stable, which matters when an operator is
//     reviewing what changed during a retry.
//   * Top-level `version: 1` field. Schema evolution is real — over a
//     few minor releases the set of fields we track will grow. The tool
//     refuses to load a file written by a future version (loud error,
//     no silent partial-load) and gracefully zero-fills fields added in
//     versions it understands.
//   * Wall-clock timestamps are written in RFC3339 UTC to match
//     Terrapod's wider convention.
//   * Sensitive variable values are NEVER written here. Only metadata
//     (key, category, sensitive flag) lands in the state file.
package framework

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// SchemaVersion is the on-disk schema the current build of the tool
// writes. Bumping this is a deliberate act: any change that drops or
// reshapes a field needs a version bump and a migration path for older
// files. Adding optional fields is back-compatible at the same version.
const SchemaVersion = 1

// DefaultStateFile is the relative path used when --state-file isn't
// passed. CWD on purpose — the operator runs the tool from a working
// directory they control, and stuffing the file in /tmp or ~ is a
// loss-of-state footgun across `apply` → `rewrite` invocations.
const DefaultStateFile = "migration-state.json"

// State is the deserialised form of the migration state file. Every
// field has explicit JSON tags so the on-disk shape is locked
// independent of Go field renames.
type State struct {
	// Version is the on-disk schema version. Always SchemaVersion on
	// fresh writes; preserved as-read on loads so a "load → modify →
	// save" cycle by an older binary doesn't silently downgrade a
	// newer file.
	Version int `json:"version"`

	// CreatedAt is the wall-clock UTC of the first `apply` invocation
	// that wrote this file. Never overwritten on subsequent saves.
	CreatedAt time.Time `json:"created_at"`

	// UpdatedAt is the wall-clock UTC of the most recent save.
	UpdatedAt time.Time `json:"updated_at"`

	// ToolVersion is the build-time-pinned version of the tool that
	// last wrote this file. Useful for support when a file ends up
	// being inspected long after the migration.
	ToolVersion string `json:"tool_version"`

	// Source names the producing plugin: "tfe" | "atlantis". Matches
	// the ir.Plan.Source field.
	Source string `json:"source"`

	// SourceHost is the source platform's API hostname — TFE/HCP's
	// `app.terraform.io` (or self-hosted equivalent) or the GitHub /
	// GitLab hostname for Atlantis's VCS reads. The `rewrite`
	// subcommand reads this to know what to rewrite from.
	SourceHost string `json:"source_host"`

	// SourceOrg is the upstream organisation name. For TFE it's the
	// org being migrated; for Atlantis it's typically empty (Atlantis
	// has no org concept). The `rewrite` subcommand reads this to
	// rewrite `organization = "..."` lines from the cloud block.
	SourceOrg string `json:"source_org,omitempty"`

	// DestHost is the Terrapod hostname the migration is targeting.
	// The `rewrite` subcommand reads this to know what to rewrite to.
	DestHost string `json:"dest_host"`

	// Workspaces is the per-workspace mapping. Sorted by SourceName on
	// every save to keep diffs stable.
	Workspaces []WorkspaceRecord `json:"workspaces,omitempty"`

	// VCSConnections is the connection mapping. One Terrapod
	// connection per source OAuth-client / PAT.
	VCSConnections []VCSConnectionRecord `json:"vcs_connections,omitempty"`

	// SkippedItems records what didn't migrate, in the order the
	// source emitted them. Surfaced in reports.
	SkippedItems []SkippedRecord `json:"skipped_items,omitempty"`

	// Subsequent increments add per-resource mappings for variable
	// sets, run triggers, notifications, agent pools, registry
	// modules, registry providers. Each gets a Record type to keep
	// the State struct narrow rather than embedding raw IR.
}

// WorkspaceRecord is what we remember about each migrated workspace.
// SourceID is the canonical upstream identifier (the TFE workspace
// UUID, or `<repo>/<dir>` for Atlantis). TerrapodID is set only after
// the workspace has actually been created on the destination — until
// then it stays empty and re-running `apply` re-attempts the create.
type WorkspaceRecord struct {
	SourceID    string    `json:"source_id"`
	SourceName  string    `json:"source_name"`
	TerrapodID  string    `json:"terrapod_id,omitempty"`
	State       string    `json:"state"` // "pending" | "created" | "errored"
	Error       string    `json:"error,omitempty"`
	StateLineage string    `json:"state_lineage,omitempty"`
	StateSerial  int64     `json:"state_serial,omitempty"`
	CreatedAt    time.Time `json:"created_at,omitzero"`
	// Labels capture the workspace's tag → label translation so the
	// rewriter can verify a cloud-block `tags = [...]` selection still
	// resolves to a migrated workspace.
	Labels map[string]string `json:"labels,omitempty"`
}

// VCSConnectionRecord — never carries credentials. Just the SourceID →
// TerrapodID mapping plus enough metadata to make the dry-run report
// readable.
type VCSConnectionRecord struct {
	SourceID   string `json:"source_id"`
	Name       string `json:"name"`
	Provider   string `json:"provider"`
	ServerURL  string `json:"server_url,omitempty"`
	TerrapodID string `json:"terrapod_id,omitempty"`
	State      string `json:"state"`
}

// SkippedRecord — operator-visible record of what we declined to
// migrate. Surfaces in the handover doc.
type SkippedRecord struct {
	Kind   string `json:"kind"`
	Name   string `json:"name"`
	Reason string `json:"reason"`
}

// ErrFutureSchema is returned by Load when the file's `version` field
// exceeds SchemaVersion. The operator action is "upgrade the tool", not
// "force-load and hope" — failing here prevents a newer file from
// being silently round-tripped through an older binary and losing
// fields the older binary doesn't know about.
var ErrFutureSchema = errors.New("migration state file written by a newer tool version; upgrade terrapod-migrate")

// ErrUnknownSchema covers v0 / negative / unparsed `version` — almost
// always corruption or a hand-edited file gone wrong.
var ErrUnknownSchema = errors.New("migration state file has an unknown schema version")

// Load reads a State from disk. Returns (nil, nil) when the file does
// not exist — that's the normal case for the first `apply` run. Other
// I/O errors and schema-version mismatches return non-nil errors.
func Load(path string) (*State, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	var s State
	if err := json.Unmarshal(data, &s); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	switch {
	case s.Version <= 0:
		return nil, fmt.Errorf("%w (version=%d)", ErrUnknownSchema, s.Version)
	case s.Version > SchemaVersion:
		return nil, fmt.Errorf("%w (file version=%d, tool understands up to %d)",
			ErrFutureSchema, s.Version, SchemaVersion)
	}
	return &s, nil
}

// Save writes the State atomically: it writes to a temp file in the
// same directory and renames over the target. Crash mid-write leaves
// the original intact rather than truncated. Permissions are 0600 —
// the state file carries no secrets but does carry the migration
// plan, and 0600 matches what operators expect of "platform state".
//
// The function also updates the housekeeping fields: Version is
// re-stamped to SchemaVersion, UpdatedAt is set to now, ToolVersion is
// set to the caller-provided value if non-empty, CreatedAt is
// preserved if already set.
func (s *State) Save(path string, toolVersion string) error {
	now := time.Now().UTC()
	if s.CreatedAt.IsZero() {
		s.CreatedAt = now
	}
	s.UpdatedAt = now
	if toolVersion != "" {
		s.ToolVersion = toolVersion
	}
	s.Version = SchemaVersion

	// MarshalIndent for human-readability — operators ARE going to
	// open this file. 2-space indent matches the Terrapod repo's
	// JSON-on-disk style (e.g. .trivyignore, helm values.schema.json).
	data, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal state: %w", err)
	}
	// Trailing newline is conventional and keeps `git diff` from
	// printing "\ No newline at end of file" noise on every save.
	data = append(data, '\n')

	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".migration-state-*.json")
	if err != nil {
		return fmt.Errorf("create temp file in %s: %w", dir, err)
	}
	tmpName := tmp.Name()
	// Best-effort cleanup if anything below fails before the rename.
	defer func() { _ = os.Remove(tmpName) }()

	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("write temp file: %w", err)
	}
	if err := tmp.Chmod(0o600); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("chmod temp file: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close temp file: %w", err)
	}
	if err := os.Rename(tmpName, path); err != nil {
		return fmt.Errorf("rename %s -> %s: %w", tmpName, path, err)
	}
	return nil
}

// WorkspaceBySourceID returns the recorded workspace by source ID, or
// nil if not present. Used by `apply` to decide whether a workspace is
// already migrated.
func (s *State) WorkspaceBySourceID(sourceID string) *WorkspaceRecord {
	for i := range s.Workspaces {
		if s.Workspaces[i].SourceID == sourceID {
			return &s.Workspaces[i]
		}
	}
	return nil
}

// WorkspaceBySourceName returns the recorded workspace by source name,
// or nil if not present. Used by the rewriter (Mode 1) to verify a
// `cloud { workspaces { name = "..." } }` block references a migrated
// workspace before rewriting it.
func (s *State) WorkspaceBySourceName(sourceName string) *WorkspaceRecord {
	for i := range s.Workspaces {
		if s.Workspaces[i].SourceName == sourceName {
			return &s.Workspaces[i]
		}
	}
	return nil
}
