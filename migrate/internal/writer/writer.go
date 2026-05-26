// Package writer translates an ir.Plan into Terrapod API calls.
//
// The writer is intentionally narrow: it knows nothing about the
// source platform (TFE vs Atlantis) — only ir.Plan items, the
// framework.State for idempotency, and go-terrapod for actual writes.
// Adding a third source platform requires zero changes here.
//
// Two modes:
//
//   * DryRun (default) — walks the Plan, builds a Report describing
//     the would-be writes, never touches Terrapod. Sensitive variable
//     values are NEVER read from the source in this mode.
//
//   * Apply — actually writes. Order is dependency-first: VCS
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

	// CredsForVCSConnection is invoked by Apply when creating a VCS
	// connection. Returns the credential payload (PrivateKey or Token)
	// the source plugin holds in memory. Sources hand the writer this
	// callback at construction; the IR never carries credentials.
	//
	// Implementations MUST return a fresh struct per call — the writer
	// doesn't cache the result. Called once per VCS connection in the
	// Plan; in DryRun mode it is NOT called (we describe the planned
	// write without ever needing the credentials).
	CredsForVCSConnection func(conn *ir.VCSConnection) (Creds, error)

	// SensitiveValueForVariable is invoked by Apply when creating a
	// sensitive variable. The IR carries the metadata (key, sensitive
	// flag) but not the value. Returning an error short-circuits the
	// affected workspace's variable writes.
	//
	// Not called in DryRun mode and not called for non-sensitive
	// variables (those carry their value in the IR).
	SensitiveValueForVariable func(workspaceSourceID, key string) (string, error)
}

// Creds is the credential payload for a single VCS connection. Only
// one of (PrivateKey, GitHub App ID, GitHub Installation ID) or Token
// is populated, depending on the provider.
type Creds struct {
	GithubAppID          int64
	GithubInstallationID int64
	PrivateKey           string

	Token string
}

// Report is the structured summary of an Apply or DryRun. It's
// surfaced to the operator both as JSON (for tooling) and as a
// rendered text summary at the end of the command output. The Report
// stays self-contained (no live SDK references) so callers can
// serialise it to disk for the handover document.
type Report struct {
	DryRun      bool                  `json:"dry_run"`
	StartedAt   time.Time             `json:"started_at"`
	FinishedAt  time.Time             `json:"finished_at"`
	Source      string                `json:"source"`
	Connections []ConnectionOutcome   `json:"connections,omitempty"`
	Workspaces  []WorkspaceOutcome    `json:"workspaces,omitempty"`
	Skipped     []ir.SkippedItem      `json:"skipped,omitempty"`
	Errors      []string              `json:"errors,omitempty"`
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
// keeps going so the operator sees the full picture).
type WorkspaceOutcome struct {
	SourceID    string         `json:"source_id"`
	Name        string         `json:"name"`
	State       string         `json:"state"` // "planned" | "created" | "reused" | "errored"
	TerrapodID  string         `json:"terrapod_id,omitempty"`
	Error       string         `json:"error,omitempty"`
	VarOutcomes []VarOutcome   `json:"var_outcomes,omitempty"`
}

// VarOutcome is the per-variable result on a workspace.
type VarOutcome struct {
	Key   string `json:"key"`
	State string `json:"state"`
	Error string `json:"error,omitempty"`
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
	// --target flag at apply time; the writer leaves the field alone
	// when not pre-set (tests don't need it).

	// VCS connections first: workspaces reference them. The map keeps
	// SourceID → TerrapodID lookups O(1) when wiring workspaces.
	connByRef := map[string]string{}
	for i := range plan.VCSConnections {
		c := &plan.VCSConnections[i]
		outcome := w.applyConnection(ctx, c, opts)
		report.Connections = append(report.Connections, outcome)
		if outcome.TerrapodID != "" {
			connByRef[c.SourceID] = outcome.TerrapodID
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

func (w *Writer) applyConnection(ctx context.Context, c *ir.VCSConnection, opts Options) ConnectionOutcome {
	out := ConnectionOutcome{
		SourceID: c.SourceID,
		Name:     c.Name,
		Provider: c.Provider,
		State:    "planned",
	}

	// Idempotency: if a prior run already created this connection,
	// reuse the recorded TerrapodID without touching the API.
	if prior := findConnectionRecord(w.state, c.SourceID); prior != nil && prior.TerrapodID != "" {
		out.State = "reused"
		out.TerrapodID = prior.TerrapodID
		return out
	}

	if opts.DryRun {
		w.recordConnection(c, "planned", "")
		return out
	}

	if opts.CredsForVCSConnection == nil {
		out.State = "errored"
		out.Error = "no credentials callback configured for apply mode"
		w.recordConnection(c, "errored", out.Error)
		return out
	}
	creds, err := opts.CredsForVCSConnection(c)
	if err != nil {
		out.State = "errored"
		out.Error = fmt.Sprintf("load credentials: %v", err)
		w.recordConnection(c, "errored", out.Error)
		return out
	}

	req := terrapod.CreateVCSConnectionRequest{
		Name:                 c.Name,
		Provider:             c.Provider,
		ServerURL:            c.ServerURL,
		GithubAppID:          creds.GithubAppID,
		GithubInstallationID: creds.GithubInstallationID,
		PrivateKey:           creds.PrivateKey,
		Token:                creds.Token,
	}
	v, err := w.client.CreateVCSConnection(ctx, req)
	if err != nil {
		out.State = "errored"
		out.Error = err.Error()
		w.recordConnection(c, "errored", out.Error)
		return out
	}

	out.State = "created"
	out.TerrapodID = v.ID
	w.recordConnection(c, "created", "")
	if rec := findConnectionRecord(w.state, c.SourceID); rec != nil {
		rec.TerrapodID = v.ID
	}
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
	// already created the workspace.
	if prior := w.state.WorkspaceBySourceID(ws.SourceID); prior != nil && prior.TerrapodID != "" {
		out.State = "reused"
		out.TerrapodID = prior.TerrapodID
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
		if id, ok := connByRef[ws.VCSConnectionRef]; ok {
			req.VCSConnectionID = id
		} else if rec := findConnectionRecord(w.state, ws.VCSConnectionRef); rec != nil {
			req.VCSConnectionID = rec.TerrapodID
		} else {
			out.State = "errored"
			out.Error = fmt.Sprintf("vcs_connection_ref %q not found in plan or state", ws.VCSConnectionRef)
			w.recordWorkspace(ws, "errored", out.Error)
			return out
		}
	}

	created, err := w.client.CreateWorkspace(ctx, req)
	if err != nil {
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
	}

	for i := range ws.Variables {
		v := &ws.Variables[i]
		vout := w.applyVariable(ctx, created.ID, ws.SourceID, v, opts)
		out.VarOutcomes = append(out.VarOutcomes, vout)
	}

	return out
}

func (w *Writer) applyVariable(ctx context.Context, workspaceID, workspaceSourceID string, v *ir.Variable, opts Options) VarOutcome {
	out := VarOutcome{Key: v.Key, State: "planned"}

	value := v.Value
	if v.Sensitive {
		if opts.SensitiveValueForVariable == nil {
			out.State = "errored"
			out.Error = "no sensitive-value callback for apply mode"
			return out
		}
		s, err := opts.SensitiveValueForVariable(workspaceSourceID, v.Key)
		if err != nil {
			out.State = "errored"
			out.Error = fmt.Sprintf("load sensitive value: %v", err)
			return out
		}
		value = s
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
		out.State = "errored"
		out.Error = err.Error()
		// 409 conflict from the server (Terrapod already has the key
		// from a prior partial run) is treated as success — variable
		// re-creation is idempotent from the operator's perspective.
		var conflict *terrapod.ConflictError
		if errors.As(err, &conflict) {
			out.State = "reused"
			out.Error = ""
		}
		return out
	}

	out.State = "created"
	return out
}

// ── State plumbing ───────────────────────────────────────────────────

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
		return
	}
	rec := framework.WorkspaceRecord{
		SourceID:   ws.SourceID,
		SourceName: ws.Name,
		State:      state,
		Error:      errMsg,
		Labels:     ws.Labels,
		CreatedAt:  time.Now().UTC(),
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
		if c.Error != "" {
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
	}
	return errs
}

