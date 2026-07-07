// Package writer translates an ir.Plan into Terrapod API calls.
//
// The writer is intentionally narrow: it knows nothing about the
// source platform (TFE vs Atlantis) — only ir.Plan items, the
// framework.State for idempotency, and go-terrapod for actual writes.
// Adding a third source platform requires zero changes here.
//
// Two modes:
//
//   - DryRun (default) — walks the Plan, builds a Report describing
//     the would-be writes, never touches Terrapod. Sensitive variable
//     values are NEVER read from the source in this mode.
//
//   - Apply — actually writes. Order is dependency-first: VCS
//     connections → workspaces → variables. After each write the state
//     file is saved so a crash mid-migration is resumable from the
//     same state file.
package writer

import (
	"context"
	"errors"
	"fmt"
	"time"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/migrate/internal/framework"
	"github.com/mattrobinsonsre/terrapod/migrate/internal/ir"
)

// Options drive a writer invocation.
type Options struct {
	// DryRun=true is the default and produces a Report without writing
	// anything to Terrapod. The state file is still updated to record
	// SourceID → "(planned)" so the Report stays stable across runs.
	DryRun bool

	// ToolVersion is stamped into the state file on save. Pass the
	// build-time version of the calling binary.
	ToolVersion string

	// VCSConnectionIDByRef maps an IR VCSConnection.SourceID to a
	// Terrapod-side connection_id the operator has ALREADY created
	// on Terrapod (via the UI or terraform-provider-terrapod). The
	// migrator does not create VCS connections on the operator's
	// behalf — that would require it to authenticate against GitHub /
	// GitLab on top of everything else, which we deliberately scope
	// out.
	//
	// When a workspace references a connection that isn't in the map,
	// the writer leaves vcs_connection_id empty on the create call
	// and records a SkippedItem in the report telling the operator
	// which connection they need to wire up post-migration.
	VCSConnectionIDByRef map[string]string

	// SensitiveValueForVariable is invoked by Apply when creating a
	// sensitive variable. The IR carries the metadata (key, sensitive
	// flag) but not the value. Returning an error short-circuits the
	// affected workspace's variable writes.
	//
	// Not called in DryRun mode and not called for non-sensitive
	// variables (those carry their value in the IR).
	SensitiveValueForVariable func(workspaceSourceID, key string) (string, error)

	// StateForWorkspace is invoked by Apply after a workspace has
	// been created to upload the source-side state to Terrapod. nil
	// disables state migration (the workspace is created but state
	// is not — operators wire it up by hand). DryRun never invokes
	// the callback.
	StateForWorkspace StateReader

	// DestHost is the Terrapod hostname (just the host, no scheme)
	// the migration is writing into. Stamped onto the state file on
	// the first apply so the rewrite subcommand can derive the
	// `cloud { hostname = "..." }` value without the operator
	// passing it again.
	DestHost string
}

// Report is the structured summary of an Apply or DryRun. It's
// surfaced to the operator both as JSON (for tooling) and as a
// rendered text summary at the end of the command output. The Report
// stays self-contained (no live SDK references) so callers can
// serialise it to disk for the handover document.
type Report struct {
	DryRun       bool                `json:"dry_run"`
	StartedAt    time.Time           `json:"started_at"`
	FinishedAt   time.Time           `json:"finished_at"`
	Source       string              `json:"source"`
	Connections  []ConnectionOutcome `json:"connections,omitempty"`
	Workspaces   []WorkspaceOutcome  `json:"workspaces,omitempty"`
	VariableSets []VarsetOutcome     `json:"variable_sets,omitempty"`
	Skipped      []ir.SkippedItem    `json:"skipped,omitempty"`
	Errors       []string            `json:"errors,omitempty"`
}

// ConnectionOutcome is the per-VCS-connection result. State is
// "planned" in DryRun, otherwise "created"/"reused"/"errored".
type ConnectionOutcome struct {
	SourceID   string `json:"source_id"`
	Name       string `json:"name"`
	Provider   string `json:"provider"`
	State      string `json:"state"`
	TerrapodID string `json:"terrapod_id,omitempty"`
	Error      string `json:"error,omitempty"`
}

// WorkspaceOutcome is the per-workspace result. VarOutcomes records
// what happened to each variable; the workspace itself can succeed
// while individual variables fail (the writer records the error and
// keeps going so the operator sees the full picture). StateOutcome
// is non-nil iff the writer attempted state migration for this
// workspace (always in Apply when StateForWorkspace is set).
type WorkspaceOutcome struct {
	SourceID     string        `json:"source_id"`
	Name         string        `json:"name"`
	State        string        `json:"state"` // "planned" | "created" | "reused" | "errored"
	TerrapodID   string        `json:"terrapod_id,omitempty"`
	Error        string        `json:"error,omitempty"`
	VarOutcomes  []VarOutcome  `json:"var_outcomes,omitempty"`
	StateOutcome *StateOutcome `json:"state_outcome,omitempty"`
}

// StateOutcome is the per-workspace state migration result.
type StateOutcome struct {
	State   string `json:"state"` // "uploaded" | "no_source_state" | "skipped" | "errored"
	Serial  int64  `json:"serial,omitempty"`
	Lineage string `json:"lineage,omitempty"`
	SizeKB  int64  `json:"size_kb,omitempty"`
	Error   string `json:"error,omitempty"`
}

// VarOutcome is the per-variable result on a workspace.
type VarOutcome struct {
	Key   string `json:"key"`
	State string `json:"state"`
	Error string `json:"error,omitempty"`
}

// VarsetOutcome is the per-variable-set result. VarOutcomes records each
// variable; Assignments is how many workspace assignments landed (source
// refs resolved to migrated Terrapod workspaces); Unresolved lists source
// workspace refs outside the migration scope (assign by hand).
type VarsetOutcome struct {
	SourceID    string       `json:"source_id"`
	Name        string       `json:"name"`
	State       string       `json:"state"` // "planned" | "created" | "reused" | "errored"
	TerrapodID  string       `json:"terrapod_id,omitempty"`
	Global      bool         `json:"global,omitempty"`
	Error       string       `json:"error,omitempty"`
	VarOutcomes []VarOutcome `json:"var_outcomes,omitempty"`
	Assignments int          `json:"assignments,omitempty"`
	Unresolved  []string     `json:"unresolved_workspace_refs,omitempty"`
}

// Writer is the entry point. Construct one per migration run and call
// Run. The Writer holds the SDK client and the on-disk state file
// path; everything else is per-invocation in Options.
type Writer struct {
	client    *terrapod.Client
	state     *framework.State
	statePath string
}

// New builds a Writer. The state argument carries any prior partial
// run; pass a fresh State for the first invocation. statePath is
// where to persist progress after every step.
func New(client *terrapod.Client, state *framework.State, statePath string) *Writer {
	return &Writer{client: client, state: state, statePath: statePath}
}

// Run executes the migration described by plan and returns a Report.
// Errors from individual items are recorded in the Report rather
// than aborting the whole run — the caller decides whether to treat
// a non-empty Errors list as failure (the apply subcommand does).
//
// The returned Report is non-nil even on a returned error (which is
// reserved for setup failures like an unwritable state file).
func (w *Writer) Run(ctx context.Context, plan ir.Plan, opts Options) (*Report, error) {
	report := &Report{
		DryRun:    opts.DryRun,
		StartedAt: time.Now().UTC(),
		Source:    plan.Source,
		Skipped:   append([]ir.SkippedItem(nil), plan.Skipped...),
	}

	// Stamp the source/destination metadata onto the state file once
	// — subsequent saves preserve them. The rewriter reads SourceHost
	// and DestHost to rewrite cloud blocks; we set them here from the
	// SourceMetadata block + the SDK base URL so the operator never
	// has to pass them by hand.
	if w.state.Source == "" {
		w.state.Source = plan.Source
	}
	if w.state.SourceHost == "" {
		w.state.SourceHost = plan.SourceMetadata["host"]
	}
	if w.state.SourceOrg == "" {
		w.state.SourceOrg = plan.SourceMetadata["org"]
	}
	// DestHost is recorded in the state file from the operator's
	// --target flag at apply time so the rewrite subcommand can
	// derive `cloud { hostname }` without a second flag pass.
	if w.state.DestHost == "" && opts.DestHost != "" {
		w.state.DestHost = opts.DestHost
	}

	// VCS connections first: look up existing Terrapod connections
	// that match each source connection's repo URL. The map keeps
	// SourceID → TerrapodID lookups O(1) when wiring workspaces.
	// Connections without a match get recorded as "missing" but
	// don't block the migration — workspaces referencing them are
	// created without vcs_connection_id and the report tells the
	// operator what to wire up.
	connByRef := map[string]string{}
	for i := range plan.VCSConnections {
		c := &plan.VCSConnections[i]
		outcome := w.applyConnection(ctx, c, opts)
		report.Connections = append(report.Connections, outcome)
		if outcome.TerrapodID != "" {
			connByRef[c.SourceID] = outcome.TerrapodID
		}
		if outcome.State == "missing" {
			// Surface as a Skipped item so the cutover handover doc
			// renders it under "Manual Action Required".
			report.Skipped = append(report.Skipped, ir.SkippedItem{
				Kind:   "vcs-connection",
				Name:   c.Name,
				Reason: fmt.Sprintf("Create a Terrapod VCS connection for %s (%s), then re-run apply to wire it to the migrated workspaces.", c.ServerURL, c.Provider),
			})
		}
		if err := w.saveState(); err != nil {
			return report, fmt.Errorf("save state after connection %q: %w", c.SourceID, err)
		}
	}

	for i := range plan.Workspaces {
		ws := &plan.Workspaces[i]
		outcome := w.applyWorkspace(ctx, ws, connByRef, opts)
		report.Workspaces = append(report.Workspaces, outcome)
		if err := w.saveState(); err != nil {
			return report, fmt.Errorf("save state after workspace %q: %w", ws.SourceID, err)
		}
	}

	// Variable sets AFTER workspaces: their per-workspace assignments
	// resolve source workspace IDs to the Terrapod IDs the workspace
	// loop just recorded in the state file.
	for i := range plan.VariableSets {
		vs := &plan.VariableSets[i]
		outcome := w.applyVariableSet(ctx, vs, opts)
		report.VariableSets = append(report.VariableSets, outcome)
		if err := w.saveState(); err != nil {
			return report, fmt.Errorf("save state after variable set %q: %w", vs.SourceID, err)
		}
	}

	// Roll skipped items into the state file too so the handover doc
	// can pull from one source rather than re-reading the IR.
	w.state.SkippedItems = w.state.SkippedItems[:0]
	for _, s := range plan.Skipped {
		w.state.SkippedItems = append(w.state.SkippedItems, framework.SkippedRecord{
			Kind: s.Kind, Name: s.Name, Reason: s.Reason,
		})
	}
	if err := w.saveState(); err != nil {
		return report, fmt.Errorf("final state save: %w", err)
	}

	report.FinishedAt = time.Now().UTC()
	report.Errors = collectErrors(report)
	return report, nil
}

// ── Connection handling ───────────────────────────────────────────────

// applyConnection looks up a Terrapod-side VCS connection that
// already exists for the given source connection's URL/provider.
// The migrator deliberately does NOT create connections — that
// would require it to authenticate against the upstream VCS
// provider (GitHub App keys / GitLab PATs) which is outside the
// migration's scope. Operators create the Terrapod VCS connection
// once via UI or terraform-provider-terrapod; the migrator
// discovers it and references it by ID.
//
// When no matching Terrapod connection exists, the connection is
// recorded as "missing" — workspaces that referenced it will be
// created without a vcs_connection_id and a SkippedItem points the
// operator at the manual follow-up.
func (w *Writer) applyConnection(_ context.Context, c *ir.VCSConnection, opts Options) ConnectionOutcome {
	out := ConnectionOutcome{
		SourceID: c.SourceID,
		Name:     c.Name,
		Provider: c.Provider,
		State:    "planned",
	}

	if id, ok := opts.VCSConnectionIDByRef[c.SourceID]; ok && id != "" {
		out.State = "matched"
		out.TerrapodID = id
		w.recordConnection(c, "matched", "")
		if rec := findConnectionRecord(w.state, c.SourceID); rec != nil {
			rec.TerrapodID = id
		}
		return out
	}

	if opts.DryRun {
		w.recordConnection(c, "missing", "")
		return out
	}

	out.State = "missing"
	out.Error = fmt.Sprintf("no Terrapod VCS connection matches source %q (%s, %s) — create one in Terrapod first, then re-run migrate", c.Name, c.Provider, c.ServerURL)
	w.recordConnection(c, "missing", out.Error)
	return out
}

// ── Workspace handling ────────────────────────────────────────────────

func (w *Writer) applyWorkspace(ctx context.Context, ws *ir.Workspace, connByRef map[string]string, opts Options) WorkspaceOutcome {
	out := WorkspaceOutcome{
		SourceID: ws.SourceID,
		Name:     ws.Name,
		State:    "planned",
	}

	// Idempotency: reuse the recorded TerrapodID if a prior run
	// already created the workspace. We still re-run state and
	// variable application against the existing workspace —
	// applyState short-circuits on (lineage, serial) match and
	// applyVariable upserts.
	if prior := w.state.WorkspaceBySourceID(ws.SourceID); prior != nil && prior.TerrapodID != "" {
		out.State = "reused"
		out.TerrapodID = prior.TerrapodID
		if !opts.DryRun {
			for i := range ws.Variables {
				v := &ws.Variables[i]
				vout := w.applyVariable(ctx, prior.TerrapodID, ws.SourceID, v, opts)
				out.VarOutcomes = append(out.VarOutcomes, vout)
			}
			if opts.StateForWorkspace != nil {
				out.StateOutcome = w.applyState(ctx, prior.TerrapodID, ws.SourceID, opts.StateForWorkspace)
			}
		}
		return out
	}

	if opts.DryRun {
		w.recordWorkspace(ws, "planned", "")
		// Don't recurse into variables in dry-run — we'd otherwise
		// invoke SensitiveValueForVariable, which is exactly the side
		// effect callers want to avoid in dry-run.
		for _, v := range ws.Variables {
			out.VarOutcomes = append(out.VarOutcomes, VarOutcome{Key: v.Key, State: "planned"})
		}
		// Report the state version that WOULD migrate so the dry-run plan
		// is complete (the issue's "report exactly what would be created
		// … state versions"). This only READS the source state for its
		// metadata (lineage/serial/size) — it never uploads. Reading is
		// the same safe operation apply does; no Terrapod write happens.
		if opts.StateForWorkspace != nil {
			out.StateOutcome = w.planState(ctx, ws.SourceID, opts.StateForWorkspace)
		}
		return out
	}

	autoApply := ws.AutoApply
	req := terrapod.CreateWorkspaceRequest{
		Name:             ws.Name,
		ExecutionMode:    ws.ExecutionMode,
		TerraformVersion: ws.TerraformVersion,
		WorkingDirectory: ws.WorkingDirectory,
		AutoApply:        &autoApply,
		Labels:           ws.Labels,
		OwnerEmail:       ws.OwnerEmail,
		VCSRepoURL:       ws.VCSRepoURL,
		VCSBranch:        ws.VCSBranch,
	}
	if ws.VCSConnectionRef != "" {
		switch {
		case connByRef[ws.VCSConnectionRef] != "":
			req.VCSConnectionID = connByRef[ws.VCSConnectionRef]
		default:
			if rec := findConnectionRecord(w.state, ws.VCSConnectionRef); rec != nil && rec.TerrapodID != "" {
				req.VCSConnectionID = rec.TerrapodID
			}
			// If neither path resolved the connection, we still
			// create the workspace — just without a VCS link. The
			// connection-level outcome ("missing") already tells
			// the operator what to wire up; failing the workspace
			// would block migrations behind operator-side VCS
			// connection wiring, which goes against the design.
		}
	}

	created, err := w.client.CreateWorkspace(ctx, req)
	if err != nil {
		// A conflict here means a Terrapod workspace already exists
		// with the same name. That's a real collision the operator
		// has to resolve — pre-existing workspace from a previous
		// (non-migrator) deployment, an unrelated workspace named
		// the same, or a half-completed prior migration where the
		// state file got blown away. Surface clearly rather than
		// hiding it as a generic error.
		var conflict *terrapod.ConflictError
		if errors.As(err, &conflict) {
			out.State = "errored"
			out.Error = fmt.Sprintf("Terrapod workspace named %q already exists; resolve the name collision (rename source or delete the existing workspace) then re-run apply", ws.Name)
			w.recordWorkspace(ws, "errored", out.Error)
			return out
		}
		out.State = "errored"
		out.Error = err.Error()
		w.recordWorkspace(ws, "errored", out.Error)
		return out
	}

	out.State = "created"
	out.TerrapodID = created.ID
	w.recordWorkspace(ws, "created", "")
	if rec := w.state.WorkspaceBySourceID(ws.SourceID); rec != nil {
		rec.TerrapodID = created.ID
		// Provenance for rollback: WE created this workspace, so rollback
		// is allowed to delete it. Reused/pre-existing workspaces never
		// reach this path, so this flag is the safe delete gate.
		rec.CreatedByMigration = true
	}

	for i := range ws.Variables {
		v := &ws.Variables[i]
		vout := w.applyVariable(ctx, created.ID, ws.SourceID, v, opts)
		out.VarOutcomes = append(out.VarOutcomes, vout)
	}

	if opts.StateForWorkspace != nil {
		out.StateOutcome = w.applyState(ctx, created.ID, ws.SourceID, opts.StateForWorkspace)
	}

	return out
}

// planState reads the source state for its metadata only and reports
// what a real apply WOULD upload — it never writes to Terrapod. Used by
// the dry-run path so the plan shows the state version (serial/lineage/
// size) alongside the workspace + variables. A missing/empty source
// state is reported as "no_source_state"; a read error is reported (so
// the operator learns about an unreadable backend at plan time) but is
// non-fatal to the dry-run.
func (w *Writer) planState(ctx context.Context, sourceID string, reader StateReader) *StateOutcome {
	out := &StateOutcome{State: "planned"}
	raw, lineage, serial, err := reader(ctx, sourceID)
	if err != nil {
		var none *ErrNoStateForWorkspace
		if errors.As(err, &none) {
			out.State = "no_source_state"
			return out
		}
		out.State = "errored"
		out.Error = fmt.Sprintf("read source state: %v", err)
		return out
	}
	if len(raw) == 0 {
		out.State = "no_source_state"
		return out
	}
	out.Serial = serial
	out.Lineage = lineage
	out.SizeKB = stateSizeKB(len(raw))
	return out
}

// applyState pulls state from the source via the StateReader and
// uploads it to the just-created Terrapod workspace. Failures here
// are recorded on the StateOutcome rather than rolling back the
// workspace — the operator can re-attempt state migration without
// re-creating the workspace by re-running apply.
//
// Idempotency: if the migration state file already records a
// (lineage, serial) pair matching what the source returns, the
// upload is skipped. This avoids re-uploading hundreds of MB of
// unchanged state on each apply iteration. The check compares
// against the state file's record — not against Terrapod — so the
// migrator doesn't need to read state back from Terrapod just to
// decide whether to write.
func (w *Writer) applyState(ctx context.Context, terrapodID, sourceID string, reader StateReader) *StateOutcome {
	out := &StateOutcome{State: "skipped"}
	raw, lineage, serial, err := reader(ctx, sourceID)
	if err != nil {
		var none *ErrNoStateForWorkspace
		if errors.As(err, &none) {
			out.State = "no_source_state"
			return out
		}
		out.State = "errored"
		out.Error = fmt.Sprintf("read source state: %v", err)
		return out
	}
	if len(raw) == 0 {
		out.State = "no_source_state"
		return out
	}

	// Local-state-file idempotency: short-circuit when the recorded
	// (lineage, serial) pair matches the source. Lineage is non-empty
	// for any valid terraform state, so we gate on lineage rather
	// than serial > 0 (legitimate states can sit at serial 0).
	if rec := w.state.WorkspaceBySourceID(sourceID); rec != nil &&
		rec.StateLineage != "" &&
		rec.StateLineage == lineage && rec.StateSerial == serial {
		out.State = "unchanged"
		out.Serial = serial
		out.Lineage = lineage
		out.SizeKB = stateSizeKB(len(raw))
		return out
	}

	// Destination-side safety net: before uploading, look at what's
	// currently on Terrapod for this workspace. Three outcomes:
	//   - NotFound (fresh workspace, no state) → proceed
	//   - dest lineage differs from source → hard error (would
	//     silently displace an unrelated state)
	//   - dest serial > source serial → hard error (would roll
	//     back operator work between iterations)
	//   - other error (transient network, 5xx) → hard error (do
	//     NOT proceed; the safety net is the only guard against
	//     silent overwrite, so treating a flake as "no state" is
	//     unsafe)
	dest, err := w.client.GetCurrentStateVersion(ctx, terrapodID)
	switch {
	case err == nil && dest != nil:
		if dest.Lineage != "" && dest.Lineage != lineage {
			out.State = "errored"
			out.Error = fmt.Sprintf(
				"destination state lineage %q differs from source %q — refusing to overwrite an unrelated state; resolve manually (delete the destination workspace, or migrate to a fresh workspace name)",
				dest.Lineage, lineage)
			return out
		}
		if dest.Serial > serial {
			out.State = "errored"
			out.Error = fmt.Sprintf(
				"destination has advanced to serial %d; source serial is %d — refusing to roll back; investigate before re-running",
				dest.Serial, serial)
			return out
		}
	case terrapod.IsNotFound(err):
		// Fresh workspace, no state yet — fine.
	case err != nil:
		out.State = "errored"
		out.Error = fmt.Sprintf("destination state pre-check failed: %v — refusing to upload blind", err)
		return out
	default:
		// err == nil && dest == nil — should be unreachable since the
		// SDK contract is (value | nil-with-NotFoundError). Treat as
		// hard failure: the safety net only works when we can read
		// the destination, and a silent (nil,nil) means we can't.
		out.State = "errored"
		out.Error = "destination state pre-check returned (nil, nil) — SDK contract violation; refusing to upload blind"
		return out
	}

	sv, err := w.client.CreateAndUploadState(ctx, terrapodID, raw, terrapod.CreateStateVersionRequest{
		Serial:  serial,
		Lineage: lineage,
	})
	if err != nil {
		// 409 conflict means a state version with this exact serial
		// already exists for the workspace. Treat as `unchanged`
		// ONLY when the destination's current state matches our
		// source's lineage AND has real content uploaded. An empty
		// placeholder (state_size == 0) means a prior run's orphan
		// rollback failed; declaring success here would leave the
		// workspace pointing at zero-byte state. Surface as an
		// error so the operator can manually delete the placeholder
		// (the API's manage endpoint allows deleting empty
		// current-version rows).
		var conflict *terrapod.ConflictError
		if errors.As(err, &conflict) {
			dest, gerr := w.client.GetCurrentStateVersion(ctx, terrapodID)
			if gerr != nil || dest == nil {
				out.State = "errored"
				out.Error = fmt.Sprintf("conflict on state upload but cannot read destination to verify: %v", err)
				return out
			}
			if dest.Lineage != lineage {
				out.State = "errored"
				out.Error = fmt.Sprintf(
					"conflict on state upload: destination has serial %d with lineage %q, source has lineage %q — resolve manually",
					dest.Serial, dest.Lineage, lineage)
				return out
			}
			if dest.StateSize == 0 {
				out.State = "errored"
				out.Error = fmt.Sprintf(
					"conflict on state upload: destination state-version %s exists at serial %d but has size 0 (orphan from a prior failed upload). Delete it via `DELETE /api/terrapod/v1/state-versions/%s/manage` and re-run apply.",
					dest.ID, dest.Serial, dest.ID)
				return out
			}
			out.State = "unchanged"
			out.Serial = serial
			out.Lineage = lineage
			out.SizeKB = stateSizeKB(len(raw))
			if rec := w.state.WorkspaceBySourceID(sourceID); rec != nil {
				rec.StateLineage = lineage
				rec.StateSerial = serial
			}
			return out
		}
		out.State = "errored"
		out.Error = err.Error()
		return out
	}
	out.State = "uploaded"
	out.Serial = sv.Serial
	out.Lineage = sv.Lineage
	out.SizeKB = stateSizeKB(len(raw))
	if rec := w.state.WorkspaceBySourceID(sourceID); rec != nil {
		rec.StateLineage = sv.Lineage
		rec.StateSerial = sv.Serial
	}
	return out
}

// stateSizeKB rounds up to the nearest KB so a 200-byte state
// reports as 1 KB rather than 0 KB. Cosmetic — the report uses this
// for operator-facing size labels only.
func stateSizeKB(n int) int64 {
	if n == 0 {
		return 0
	}
	return int64((n + 1023) / 1024)
}

func (w *Writer) applyVariable(ctx context.Context, workspaceID, workspaceSourceID string, v *ir.Variable, opts Options) VarOutcome {
	out := VarOutcome{Key: v.Key, State: "planned"}

	value := v.Value
	if v.Sensitive {
		// Sensitive variable values cannot be read back from source
		// platforms (TFE returns null; Atlantis has none). The
		// migrator deliberately does NOT prompt for or store values
		// — instead the variable is created on Terrapod with an
		// empty value + sensitive=true, so the operator sees the row
		// in the workspace UI and can fill it in post-cutover. The
		// per-workspace SkippedItem emitted by the source flags
		// which keys need attention. The SensitiveValueForVariable
		// callback is an opt-in escape hatch for future value-loader
		// plugins; when set + non-error, its return value is used.
		value = ""
		out.State = "needs_value"
		if opts.SensitiveValueForVariable != nil {
			if s, err := opts.SensitiveValueForVariable(workspaceSourceID, v.Key); err == nil {
				value = s
				out.State = "planned"
			}
		}
	}

	req := terrapod.CreateVariableRequest{
		Key:         v.Key,
		Value:       value,
		Category:    v.Category,
		HCL:         v.HCL,
		Sensitive:   v.Sensitive,
		Description: v.Description,
	}
	if _, err := w.client.CreateVariable(ctx, workspaceID, req); err != nil {
		// 409 conflict from the server (Terrapod already has the
		// key) means an earlier apply already wrote it. For
		// non-sensitive vars: PATCH to reconcile the value so the
		// destination reflects the current source. For sensitive
		// vars: leave the existing value alone — the operator
		// hand-entered it post-cutover; the migrator must never
		// clobber operator-entered secrets.
		var conflict *terrapod.ConflictError
		if errors.As(err, &conflict) {
			if v.Sensitive {
				out.State = "needs_value"
				return out
			}
			if rerr := w.reconcileVariable(ctx, workspaceID, req); rerr != nil {
				out.State = "errored"
				out.Error = fmt.Sprintf("reconcile existing variable: %v", rerr)
				return out
			}
			out.State = "reconciled"
			return out
		}
		out.State = "errored"
		out.Error = err.Error()
		return out
	}

	if v.Sensitive {
		// Row created with empty value; operator must fill it in.
		// Leave State="needs_value" — set above.
		return out
	}
	out.State = "created"
	return out
}

// reconcileVariable PATCHes an existing variable to align its value,
// category, HCL and description with the source. Used on Apply-mode
// 409 to recover from a partial prior run rather than leaving the
// destination at a stale value.
//
// Safety:
//   - Sensitive source variables are filtered out by the caller —
//     reconcile is never invoked for them, so operator-entered
//     secrets are not clobbered.
//   - If the DESTINATION row is sensitive (operator hand-flagged it
//     post-cutover), reconcile refuses the PATCH — the source's
//     non-sensitive value would silently overwrite the operator's
//     secret. Surfaced as a clear error so the operator can decide.
func (w *Writer) reconcileVariable(ctx context.Context, workspaceID string, req terrapod.CreateVariableRequest) error {
	existing, err := w.client.GetVariableByKey(ctx, workspaceID, req.Key)
	if err != nil {
		return fmt.Errorf("locate existing %q: %w", req.Key, err)
	}
	if existing.Sensitive {
		return fmt.Errorf("destination variable %q is flagged sensitive — refusing to overwrite an operator-entered value with the source's non-sensitive value", req.Key)
	}
	hcl := req.HCL
	updReq := terrapod.UpdateVariableRequest{
		Value:       &req.Value,
		Category:    req.Category,
		HCL:         &hcl,
		Description: &req.Description,
	}
	if _, err := w.client.UpdateVariable(ctx, workspaceID, existing.ID, updReq); err != nil {
		return fmt.Errorf("patch %q: %w", req.Key, err)
	}
	return nil
}

// ── State plumbing ───────────────────────────────────────────────────

// ── Variable set handling ─────────────────────────────────────────────

// applyVariableSet creates a variable set on Terrapod, adds its
// variables, and assigns it to the migrated workspaces it referenced.
// Runs after the workspace loop so WorkspaceRefs (source IDs) resolve to
// the Terrapod workspace IDs recorded during that loop. Idempotent: a
// prior run's varset (recorded with a TerrapodID) is reused, not
// re-created.
func (w *Writer) applyVariableSet(ctx context.Context, vs *ir.VariableSet, opts Options) VarsetOutcome {
	out := VarsetOutcome{SourceID: vs.SourceID, Name: vs.Name, State: "planned", Global: vs.Global}

	if prior := w.state.VarsetBySourceID(vs.SourceID); prior != nil && prior.TerrapodID != "" {
		out.State = "reused"
		out.TerrapodID = prior.TerrapodID
		if !opts.DryRun {
			w.applyVarsetContents(ctx, prior.TerrapodID, vs, &out, opts)
		}
		return out
	}

	if opts.DryRun {
		// Don't recurse into variables (would invoke the sensitive-value
		// callback) — just plan them, mirroring the workspace path.
		for _, v := range vs.Variables {
			out.VarOutcomes = append(out.VarOutcomes, VarOutcome{Key: v.Key, State: "planned"})
		}
		out.Assignments, out.Unresolved = w.planVarsetAssignments(vs)
		w.recordVarset(vs, "planned", "", 0)
		return out
	}

	created, err := w.client.CreateVariableSet(ctx, terrapod.CreateVariableSetRequest{
		Name:        vs.Name,
		Description: vs.Description,
		Global:      vs.Global,
		Priority:    vs.Priority,
	})
	if err != nil {
		var conflict *terrapod.ConflictError
		if errors.As(err, &conflict) {
			out.State = "errored"
			out.Error = fmt.Sprintf("Terrapod variable set named %q already exists; resolve the collision (rename or delete the existing set) then re-run apply", vs.Name)
			w.recordVarset(vs, "errored", out.Error, 0)
			return out
		}
		out.State = "errored"
		out.Error = err.Error()
		w.recordVarset(vs, "errored", out.Error, 0)
		return out
	}

	out.State = "created"
	out.TerrapodID = created.ID
	w.recordVarset(vs, "created", "", len(vs.Variables))
	if rec := w.state.VarsetBySourceID(vs.SourceID); rec != nil {
		rec.TerrapodID = created.ID
		// Provenance gate for a future rollback: WE created this set.
		rec.CreatedByMigration = true
	}
	w.applyVarsetContents(ctx, created.ID, vs, &out, opts)
	return out
}

// applyVarsetContents adds the variables and workspace assignments to an
// existing (just-created or reused) varset.
func (w *Writer) applyVarsetContents(ctx context.Context, varsetID string, vs *ir.VariableSet, out *VarsetOutcome, opts Options) {
	for i := range vs.Variables {
		v := &vs.Variables[i]
		out.VarOutcomes = append(out.VarOutcomes, w.applyVarsetVariable(ctx, varsetID, vs.SourceID, v, opts))
	}

	// Global sets apply to everything and take no explicit assignment.
	if vs.Global {
		return
	}
	for _, ref := range vs.WorkspaceRefs {
		rec := w.state.WorkspaceBySourceID(ref)
		if rec == nil || rec.TerrapodID == "" {
			// The referenced workspace wasn't migrated (out of scope, or
			// it errored). Surface it so the operator assigns by hand.
			out.Unresolved = append(out.Unresolved, ref)
			continue
		}
		if err := w.client.AssignWorkspaceToVarset(ctx, varsetID, rec.TerrapodID); err != nil {
			var conflict *terrapod.ConflictError
			if errors.As(err, &conflict) {
				out.Assignments++ // already assigned by a prior run
				continue
			}
			out.Unresolved = append(out.Unresolved, ref)
			continue
		}
		out.Assignments++
	}
	if rec := w.state.VarsetBySourceID(vs.SourceID); rec != nil {
		rec.AssignedWorkspaces = out.Assignments
	}
}

// applyVarsetVariable adds one variable to a varset. Mirrors
// applyVariable's sensitive-value handling: sensitive values are never
// read from the source — the variable is created with an empty value +
// sensitive=true for the operator to fill in post-cutover.
func (w *Writer) applyVarsetVariable(ctx context.Context, varsetID, varsetSourceID string, v *ir.Variable, opts Options) VarOutcome {
	out := VarOutcome{Key: v.Key, State: "created"}

	value := v.Value
	if v.Sensitive {
		value = ""
		out.State = "needs_value"
		if opts.SensitiveValueForVariable != nil {
			if s, err := opts.SensitiveValueForVariable(varsetSourceID, v.Key); err == nil {
				value = s
				out.State = "created"
			}
		}
	}

	_, err := w.client.CreateVarsetVariable(ctx, varsetID, terrapod.CreateVarsetVariableRequest{
		Key:         v.Key,
		Value:       value,
		Category:    v.Category,
		HCL:         v.HCL,
		Sensitive:   v.Sensitive,
		Description: v.Description,
	})
	if err != nil {
		// 409 → a prior run already added this key. Leave sensitive
		// values alone (operator may have hand-entered them); treat
		// non-sensitive as reconciled (present, value not re-pushed in
		// this slice).
		var conflict *terrapod.ConflictError
		if errors.As(err, &conflict) {
			if v.Sensitive {
				out.State = "needs_value"
				return out
			}
			out.State = "reconciled"
			return out
		}
		out.State = "errored"
		out.Error = err.Error()
		return out
	}
	return out
}

// planVarsetAssignments reports, for a dry-run, how many of a varset's
// workspace refs resolve to a recorded (planned or created) workspace
// and which don't. Global sets return (0, nil).
func (w *Writer) planVarsetAssignments(vs *ir.VariableSet) (int, []string) {
	if vs.Global {
		return 0, nil
	}
	var resolved int
	var unresolved []string
	for _, ref := range vs.WorkspaceRefs {
		if w.state.WorkspaceBySourceID(ref) != nil {
			resolved++
		} else {
			unresolved = append(unresolved, ref)
		}
	}
	return resolved, unresolved
}

func (w *Writer) recordVarset(vs *ir.VariableSet, state, errMsg string, varCount int) {
	if rec := w.state.VarsetBySourceID(vs.SourceID); rec != nil {
		rec.State = state
		rec.Error = errMsg
		if varCount > 0 {
			rec.ExpectedVarCount = varCount
		}
		return
	}
	w.state.VariableSets = append(w.state.VariableSets, framework.VariableSetRecord{
		SourceID:         vs.SourceID,
		Name:             vs.Name,
		State:            state,
		Error:            errMsg,
		ExpectedVarCount: varCount,
	})
}

func (w *Writer) recordConnection(c *ir.VCSConnection, state, errMsg string) {
	if rec := findConnectionRecord(w.state, c.SourceID); rec != nil {
		rec.State = state
		return
	}
	w.state.VCSConnections = append(w.state.VCSConnections, framework.VCSConnectionRecord{
		SourceID:  c.SourceID,
		Name:      c.Name,
		Provider:  c.Provider,
		ServerURL: c.ServerURL,
		State:     state,
	})
}

func (w *Writer) recordWorkspace(ws *ir.Workspace, state, errMsg string) {
	if rec := w.state.WorkspaceBySourceID(ws.SourceID); rec != nil {
		rec.State = state
		rec.Error = errMsg
		rec.ExpectedVarCount = len(ws.Variables)
		return
	}
	rec := framework.WorkspaceRecord{
		SourceID:         ws.SourceID,
		SourceName:       ws.Name,
		State:            state,
		Error:            errMsg,
		Labels:           ws.Labels,
		ExpectedVarCount: len(ws.Variables),
		CreatedAt:        time.Now().UTC(),
	}
	w.state.Workspaces = append(w.state.Workspaces, rec)
}

func (w *Writer) saveState() error {
	if w.statePath == "" {
		return nil // in-memory only (tests)
	}
	return w.state.Save(w.statePath, "")
}

// ── Helpers ──────────────────────────────────────────────────────────

func findConnectionRecord(s *framework.State, sourceID string) *framework.VCSConnectionRecord {
	for i := range s.VCSConnections {
		if s.VCSConnections[i].SourceID == sourceID {
			return &s.VCSConnections[i]
		}
	}
	return nil
}

func collectErrors(r *Report) []string {
	var errs []string
	for _, c := range r.Connections {
		// "missing" is operator-actionable but not a writer failure —
		// the workspace is still created without a VCS connection
		// and the report's Skipped section tells the operator how
		// to wire it up. Only true errors land here.
		if c.Error != "" && c.State != "missing" {
			errs = append(errs, fmt.Sprintf("vcs-connection %q: %s", c.Name, c.Error))
		}
	}
	for _, ws := range r.Workspaces {
		if ws.Error != "" {
			errs = append(errs, fmt.Sprintf("workspace %q: %s", ws.Name, ws.Error))
		}
		for _, v := range ws.VarOutcomes {
			if v.Error != "" {
				errs = append(errs, fmt.Sprintf("workspace %q variable %q: %s", ws.Name, v.Key, v.Error))
			}
		}
		if ws.StateOutcome != nil && ws.StateOutcome.Error != "" {
			errs = append(errs, fmt.Sprintf("workspace %q state: %s", ws.Name, ws.StateOutcome.Error))
		}
	}
	for _, vs := range r.VariableSets {
		if vs.Error != "" {
			errs = append(errs, fmt.Sprintf("variable-set %q: %s", vs.Name, vs.Error))
		}
		for _, v := range vs.VarOutcomes {
			if v.Error != "" {
				errs = append(errs, fmt.Sprintf("variable-set %q variable %q: %s", vs.Name, v.Key, v.Error))
			}
		}
	}
	return errs
}
