package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/migrate/internal/framework"
)

// rollbackCmd reverses what a migration `apply` created. It reads the
// migration state file and deletes the Terrapod workspaces AND variable
// sets THIS migration created — making a migration reversible, which is
// what makes it approvable: "we can undo it" removes the largest
// switching-cost objection. Variable sets are deleted first (the reverse
// of the create order, workspaces → varsets); they are org-level config
// with no state serial, so — unlike workspaces — they have no
// advanced-state guard, only the provenance gate.
//
// Safety (this DELETES infrastructure-state-bearing resources, so it is
// guarded like the apply path):
//
//   - Dry-run by DEFAULT. Pass --apply to actually delete. The dry-run
//     prints exactly which workspaces would be deleted and which are
//     left untouched.
//   - Provenance gate: deletes ONLY workspaces the migration itself
//     created (state record CreatedByMigration). Workspaces the
//     migration merely reused — anything that pre-existed, including
//     `apply --workspace` direct targets — are NEVER deleted. Older
//     state files without provenance decode to "not created by us" and
//     are skipped.
//   - Advanced-state guard: before deleting, it reads the workspace's
//     current state serial. If the workspace has advanced PAST the
//     serial the migration recorded — i.e. someone has applied real
//     changes since the migration — it is skipped (the operator must
//     pass --force to delete a workspace that's been used). A
//     destination read that fails is treated as "don't delete blind"
//     unless --force.
//   - VCS connections are never touched: the migrator only ever matched
//     pre-existing, operator-owned connections, so they are reported as
//     left in place.
//   - Idempotent: a workspace already gone (404) is recorded as
//     rolled_back; re-running is safe.
func rollbackCmd(args []string) int {
	fs := flag.NewFlagSet("rollback", flag.ContinueOnError)
	var (
		target    = fs.String("target", os.Getenv("TERRAPOD_HOSTNAME"), "Terrapod base URL (or TERRAPOD_HOSTNAME)")
		token     = fs.String("token", os.Getenv("TERRAPOD_TOKEN"), "Terrapod API token (or TERRAPOD_TOKEN)")
		statePath = fs.String("state-file", framework.DefaultStateFile, "Path to the migration state JSON file")
		apply     = fs.Bool("apply", false, "Actually delete (default is dry-run)")
		force     = fs.Bool("force", false, "Delete even workspaces whose state has advanced past the migrated serial (DANGEROUS — destroys post-migration work)")
		jsonOut   = fs.Bool("json", false, "Emit the rollback report as JSON")
		skipTLS   = fs.Bool("skip-tls-verify", false, "Skip TLS certificate verification (dev only)")
	)
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *target == "" {
		fmt.Fprintln(os.Stderr, "rollback: --target (or TERRAPOD_HOSTNAME) is required")
		return 2
	}
	if *token == "" {
		fmt.Fprintln(os.Stderr, "rollback: --token (or TERRAPOD_TOKEN) is required")
		return 2
	}

	state, err := framework.Load(*statePath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "rollback: load state file %s: %v\n", *statePath, err)
		return 1
	}
	if state == nil {
		fmt.Fprintf(os.Stderr, "rollback: state file %s not found — nothing to roll back\n", *statePath)
		return 1
	}

	c, err := terrapod.NewClient(terrapod.Options{
		BaseURL:       *target,
		Token:         *token,
		SkipTLSVerify: *skipTLS,
		UserAgent:     "terrapod-migrate/" + Version,
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "rollback: build terrapod client: %v\n", err)
		return 1
	}
	warnVersionMismatch(c)

	report := runRollback(context.Background(), c, state, *statePath, *apply, *force)

	if *jsonOut {
		if data, err := json.MarshalIndent(report, "", "  "); err == nil {
			fmt.Println(string(data))
		}
	} else {
		printRollbackSummary(report, !*apply)
	}
	if len(report.Errors) > 0 {
		return 1
	}
	return 0
}

// RollbackReport is the structured result of a rollback run.
type RollbackReport struct {
	DryRun              bool                   `json:"dry_run"`
	Target              string                 `json:"target"`
	GPGKeys             []RollbackGPGKey       `json:"gpg_keys,omitempty"`
	AgentPools          []RollbackAgentPool    `json:"agent_pools,omitempty"`
	Notifications       []RollbackNotification `json:"notifications,omitempty"`
	RunTriggers         []RollbackRunTrigger   `json:"run_triggers,omitempty"`
	VariableSets        []RollbackVarset       `json:"variable_sets,omitempty"`
	Workspaces          []RollbackWorkspace    `json:"workspaces"`
	ConnectionsLeft     []string               `json:"connections_left_in_place,omitempty"`
	DeletedCount        int                    `json:"deleted_count"`
	VarsetDeleted       int                    `json:"varset_deleted_count"`
	RunTriggerDeleted   int                    `json:"run_trigger_deleted_count"`
	NotificationDeleted int                    `json:"notification_deleted_count"`
	AgentPoolDeleted    int                    `json:"agent_pool_deleted_count"`
	GPGKeyDeleted       int                    `json:"gpg_key_deleted_count"`
	SkippedCount        int                    `json:"skipped_count"`
	Errors              []string               `json:"errors,omitempty"`
}

// RollbackGPGKey is the per-GPG-key rollback outcome. Action is one of:
// "would_delete" (dry-run), "deleted", "already_gone", "errored".
type RollbackGPGKey struct {
	KeyID      string `json:"key_id"`
	TerrapodID string `json:"terrapod_id,omitempty"`
	Action     string `json:"action"`
	Detail     string `json:"detail,omitempty"`
}

// RollbackAgentPool is the per-agent-pool rollback outcome. Action is
// one of: "would_delete" (dry-run), "deleted", "already_gone",
// "errored". Deleting a pool SET-NULLs any workspace still pointing at
// it, so there's no ordering hazard with the workspace deletes.
type RollbackAgentPool struct {
	Name       string `json:"name"`
	TerrapodID string `json:"terrapod_id,omitempty"`
	Action     string `json:"action"`
	Detail     string `json:"detail,omitempty"`
}

// RollbackNotification is the per-notification-config rollback outcome.
// Action is one of: "would_delete" (dry-run), "deleted", "already_gone",
// "errored".
type RollbackNotification struct {
	Workspace  string `json:"workspace"` // destination workspace ref
	Name       string `json:"name"`
	TerrapodID string `json:"terrapod_id,omitempty"`
	Action     string `json:"action"`
	Detail     string `json:"detail,omitempty"`
}

// RollbackVarset is the per-variable-set rollback outcome. Action is one
// of: "would_delete" (dry-run), "deleted", "already_gone", "errored".
type RollbackVarset struct {
	Name       string `json:"name"`
	TerrapodID string `json:"terrapod_id,omitempty"`
	Action     string `json:"action"`
	Detail     string `json:"detail,omitempty"`
}

// RollbackRunTrigger is the per-run-trigger rollback outcome.
type RollbackRunTrigger struct {
	Pair       string `json:"pair"` // "source_ref → destination_ref"
	TerrapodID string `json:"terrapod_id,omitempty"`
	Action     string `json:"action"`
	Detail     string `json:"detail,omitempty"`
}

// RollbackWorkspace is the per-workspace rollback outcome. Action is one
// of: "would_delete" (dry-run), "deleted", "already_gone",
// "skipped_advanced", "skipped_not_created", "errored".
type RollbackWorkspace struct {
	SourceName     string `json:"source_name"`
	TerrapodID     string `json:"terrapod_id,omitempty"`
	Action         string `json:"action"`
	MigratedSerial int64  `json:"migrated_serial"`
	CurrentSerial  int64  `json:"current_serial,omitempty"`
	Detail         string `json:"detail,omitempty"`
}

func runRollback(ctx context.Context, c *terrapod.Client, state *framework.State, statePath string, apply, force bool) *RollbackReport {
	report := &RollbackReport{DryRun: !apply, Target: state.DestHost}

	// Connections are operator-owned (the migrator only matched
	// pre-existing ones); never delete them. Report them as left in place.
	for i := range state.VCSConnections {
		report.ConnectionsLeft = append(report.ConnectionsLeft, state.VCSConnections[i].Name)
	}

	// GPG keys first — provider signing PUBLIC keys, created last and
	// independent of everything else, so they lead the reverse order.
	// Pure config with no dependents; the provenance gate is the whole
	// safety boundary. Delete is 404-tolerant.
	for _, rec := range state.GPGKeyRollbackTargets() {
		gk := RollbackGPGKey{KeyID: rec.KeyID, TerrapodID: rec.TerrapodID}
		if !apply {
			gk.Action = "would_delete"
			report.GPGKeys = append(report.GPGKeys, gk)
			report.GPGKeyDeleted++
			continue
		}
		if err := c.DeleteGPGKey(ctx, rec.TerrapodID); err != nil {
			var nf *terrapod.NotFoundError
			if errors.As(err, &nf) {
				gk.Action = "already_gone"
				rec.State = "rolled_back"
				rec.TerrapodID = ""
				report.GPGKeys = append(report.GPGKeys, gk)
				report.GPGKeyDeleted++
				_ = state.Save(statePath, Version)
				continue
			}
			gk.Action = "errored"
			gk.Detail = err.Error()
			report.GPGKeys = append(report.GPGKeys, gk)
			report.Errors = append(report.Errors, fmt.Sprintf("gpg-key %q: delete failed: %v", gk.KeyID, err))
			continue
		}
		gk.Action = "deleted"
		rec.State = "rolled_back"
		rec.TerrapodID = ""
		report.GPGKeys = append(report.GPGKeys, gk)
		report.GPGKeyDeleted++
		if err := state.Save(statePath, Version); err != nil {
			report.Errors = append(report.Errors, fmt.Sprintf("gpg-key %q deleted but state save failed: %v", gk.KeyID, err))
		}
	}

	// Agent pools next — they were created after notifications (workspaces
	// → varsets → run triggers → notifications → agent pools → gpg keys),
	// so rollback deletes them after gpg keys. Deleting a pool SET-NULLs
	// any workspace still pointing at it (FK ondelete=SET NULL), so there
	// is no ordering hazard with the workspace deletes that follow. The
	// provenance gate is the whole safety boundary. Delete is 404-tolerant.
	for _, rec := range state.AgentPoolRollbackTargets() {
		ap := RollbackAgentPool{Name: rec.Name, TerrapodID: rec.TerrapodID}
		if !apply {
			ap.Action = "would_delete"
			report.AgentPools = append(report.AgentPools, ap)
			report.AgentPoolDeleted++
			continue
		}
		if err := c.DeleteAgentPool(ctx, rec.TerrapodID); err != nil {
			var nf *terrapod.NotFoundError
			if errors.As(err, &nf) {
				ap.Action = "already_gone"
				rec.State = "rolled_back"
				rec.TerrapodID = ""
				report.AgentPools = append(report.AgentPools, ap)
				report.AgentPoolDeleted++
				_ = state.Save(statePath, Version)
				continue
			}
			ap.Action = "errored"
			ap.Detail = err.Error()
			report.AgentPools = append(report.AgentPools, ap)
			report.Errors = append(report.Errors, fmt.Sprintf("agent-pool %q: delete failed: %v", ap.Name, err))
			continue
		}
		ap.Action = "deleted"
		rec.State = "rolled_back"
		rec.TerrapodID = ""
		report.AgentPools = append(report.AgentPools, ap)
		report.AgentPoolDeleted++
		if err := state.Save(statePath, Version); err != nil {
			report.Errors = append(report.Errors, fmt.Sprintf("agent-pool %q deleted but state save failed: %v", ap.Name, err))
		}
	}

	// Notifications next — created after run triggers, so deleted before
	// them in the reverse order. Pure per-workspace config with no state
	// serial, so no advanced-state guard; the provenance gate is the whole
	// safety boundary. Delete is 404-tolerant.
	for _, rec := range state.NotificationRollbackTargets() {
		nt := RollbackNotification{
			Workspace:  rec.WorkspaceRef,
			Name:       rec.Name,
			TerrapodID: rec.TerrapodID,
		}
		if !apply {
			nt.Action = "would_delete"
			report.Notifications = append(report.Notifications, nt)
			report.NotificationDeleted++
			continue
		}
		if err := c.DeleteNotificationConfiguration(ctx, rec.TerrapodID); err != nil {
			var nf *terrapod.NotFoundError
			if errors.As(err, &nf) {
				nt.Action = "already_gone"
				rec.State = "rolled_back"
				rec.TerrapodID = ""
				report.Notifications = append(report.Notifications, nt)
				report.NotificationDeleted++
				_ = state.Save(statePath, Version)
				continue
			}
			nt.Action = "errored"
			nt.Detail = err.Error()
			report.Notifications = append(report.Notifications, nt)
			report.Errors = append(report.Errors, fmt.Sprintf("notification %q on %s: delete failed: %v", nt.Name, nt.Workspace, err))
			continue
		}
		nt.Action = "deleted"
		rec.State = "rolled_back"
		rec.TerrapodID = ""
		report.Notifications = append(report.Notifications, nt)
		report.NotificationDeleted++
		if err := state.Save(statePath, Version); err != nil {
			report.Errors = append(report.Errors, fmt.Sprintf("notification %q on %s deleted but state save failed: %v", nt.Name, nt.Workspace, err))
		}
	}

	// Run triggers next — the reverse of the create order (workspaces →
	// varsets → run triggers). Like varsets they're pure config with no
	// state serial, so no advanced-state guard; the provenance gate is
	// the whole safety boundary. Delete is 404-tolerant.
	for _, rec := range state.RunTriggerRollbackTargets() {
		rt := RollbackRunTrigger{
			Pair:       fmt.Sprintf("%s → %s", rec.SourceWorkspaceRef, rec.DestinationWorkspaceRef),
			TerrapodID: rec.TerrapodID,
		}
		if !apply {
			rt.Action = "would_delete"
			report.RunTriggers = append(report.RunTriggers, rt)
			report.RunTriggerDeleted++
			continue
		}
		if err := c.DeleteRunTrigger(ctx, rec.TerrapodID); err != nil {
			var nf *terrapod.NotFoundError
			if errors.As(err, &nf) {
				rt.Action = "already_gone"
				rec.State = "rolled_back"
				rec.TerrapodID = ""
				report.RunTriggers = append(report.RunTriggers, rt)
				report.RunTriggerDeleted++
				_ = state.Save(statePath, Version)
				continue
			}
			rt.Action = "errored"
			rt.Detail = err.Error()
			report.RunTriggers = append(report.RunTriggers, rt)
			report.Errors = append(report.Errors, fmt.Sprintf("run-trigger %s: delete failed: %v", rt.Pair, err))
			continue
		}
		rt.Action = "deleted"
		rec.State = "rolled_back"
		rec.TerrapodID = ""
		report.RunTriggers = append(report.RunTriggers, rt)
		report.RunTriggerDeleted++
		if err := state.Save(statePath, Version); err != nil {
			report.Errors = append(report.Errors, fmt.Sprintf("run-trigger %s deleted but state save failed: %v", rt.Pair, err))
		}
	}

	// Variable sets next — the reverse of the create order (workspaces →
	// varsets). Varsets are org-level config with no state serial, so
	// there is no advanced-state guard; the provenance gate is the whole
	// safety boundary. Delete is 404-tolerant so re-runs stay clean.
	for _, rec := range state.VarsetRollbackTargets() {
		rv := RollbackVarset{Name: rec.Name, TerrapodID: rec.TerrapodID}
		if !apply {
			rv.Action = "would_delete"
			report.VariableSets = append(report.VariableSets, rv)
			report.VarsetDeleted++ // count of would-be deletions in dry-run
			continue
		}
		if err := c.DeleteVariableSet(ctx, rec.TerrapodID); err != nil {
			var nf *terrapod.NotFoundError
			if errors.As(err, &nf) {
				rv.Action = "already_gone"
				rec.State = "rolled_back"
				rec.TerrapodID = ""
				report.VariableSets = append(report.VariableSets, rv)
				report.VarsetDeleted++
				_ = state.Save(statePath, Version)
				continue
			}
			rv.Action = "errored"
			rv.Detail = err.Error()
			report.VariableSets = append(report.VariableSets, rv)
			report.Errors = append(report.Errors, fmt.Sprintf("variable-set %q: delete failed: %v", rec.Name, err))
			continue
		}
		rv.Action = "deleted"
		rec.State = "rolled_back"
		rec.TerrapodID = ""
		report.VariableSets = append(report.VariableSets, rv)
		report.VarsetDeleted++
		if err := state.Save(statePath, Version); err != nil {
			report.Errors = append(report.Errors, fmt.Sprintf("variable-set %q deleted but state save failed: %v", rec.Name, err))
		}
	}

	targets := state.RollbackTargets()
	for _, rec := range targets {
		rw := RollbackWorkspace{
			SourceName:     rec.SourceName,
			TerrapodID:     rec.TerrapodID,
			MigratedSerial: rec.StateSerial,
		}

		if !apply {
			rw.Action = "would_delete"
			report.Workspaces = append(report.Workspaces, rw)
			report.DeletedCount++ // count of would-be deletions in dry-run
			continue
		}

		// Advanced-state guard: read the destination's current serial.
		// Skip (unless --force) if it advanced past what we migrated —
		// that means real work happened on the workspace post-migration
		// and deleting it would destroy it.
		dest, derr := c.GetCurrentStateVersion(ctx, rec.TerrapodID)
		switch {
		case derr == nil && dest != nil:
			rw.CurrentSerial = dest.Serial
			if dest.Serial > rec.StateSerial && !force {
				rw.Action = "skipped_advanced"
				rw.Detail = fmt.Sprintf("current serial %d > migrated serial %d — workspace has been used since migration; re-run with --force to delete anyway", dest.Serial, rec.StateSerial)
				report.Workspaces = append(report.Workspaces, rw)
				report.SkippedCount++
				continue
			}
		case terrapod.IsNotFound(derr):
			// No state yet (or workspace already gone). Either way safe
			// to proceed to the delete, which is itself 404-tolerant.
		case derr != nil && !force:
			rw.Action = "errored"
			rw.Detail = fmt.Sprintf("could not read destination state to confirm it's safe to delete: %v — refusing to delete blind (use --force to override)", derr)
			report.Workspaces = append(report.Workspaces, rw)
			report.Errors = append(report.Errors, fmt.Sprintf("workspace %q: %s", rec.SourceName, rw.Detail))
			continue
		}

		if err := c.DeleteWorkspace(ctx, rec.TerrapodID); err != nil {
			var nf *terrapod.NotFoundError
			if errors.As(err, &nf) {
				// Already gone — record as rolled back so re-runs are clean.
				rw.Action = "already_gone"
				rec.State = "rolled_back"
				rec.TerrapodID = ""
				report.Workspaces = append(report.Workspaces, rw)
				report.DeletedCount++
				_ = state.Save(statePath, Version)
				continue
			}
			rw.Action = "errored"
			rw.Detail = err.Error()
			report.Workspaces = append(report.Workspaces, rw)
			report.Errors = append(report.Errors, fmt.Sprintf("workspace %q: delete failed: %v", rec.SourceName, err))
			continue
		}

		rw.Action = "deleted"
		rec.State = "rolled_back"
		rec.TerrapodID = ""
		report.Workspaces = append(report.Workspaces, rw)
		report.DeletedCount++
		// Persist after every delete so an interrupted rollback resumes
		// cleanly (a re-run skips already-rolled-back records).
		if err := state.Save(statePath, Version); err != nil {
			report.Errors = append(report.Errors, fmt.Sprintf("workspace %q deleted but state save failed: %v", rec.SourceName, err))
		}
	}

	return report
}

func printRollbackSummary(r *RollbackReport, dryRun bool) {
	label := "rolled back"
	if dryRun {
		label = "planned (dry-run; pass --apply to delete)"
	}
	fmt.Printf("\nterrapod-migrate rollback — %s\n", label)
	fmt.Printf("  target:        %s\n", r.Target)
	if dryRun {
		fmt.Printf("  would delete:  %d workspace(s)\n", r.DeletedCount)
	} else {
		fmt.Printf("  deleted:       %d\n", r.DeletedCount)
		fmt.Printf("  skipped:       %d\n", r.SkippedCount)
	}
	if r.VarsetDeleted > 0 {
		if dryRun {
			fmt.Printf("  would delete:  %d variable set(s)\n", r.VarsetDeleted)
		} else {
			fmt.Printf("  variable sets deleted: %d\n", r.VarsetDeleted)
		}
	}
	if r.RunTriggerDeleted > 0 {
		if dryRun {
			fmt.Printf("  would delete:  %d run trigger(s)\n", r.RunTriggerDeleted)
		} else {
			fmt.Printf("  run triggers deleted: %d\n", r.RunTriggerDeleted)
		}
	}
	if r.NotificationDeleted > 0 {
		if dryRun {
			fmt.Printf("  would delete:  %d notification config(s)\n", r.NotificationDeleted)
		} else {
			fmt.Printf("  notification configs deleted: %d\n", r.NotificationDeleted)
		}
	}
	if r.AgentPoolDeleted > 0 {
		if dryRun {
			fmt.Printf("  would delete:  %d agent pool(s)\n", r.AgentPoolDeleted)
		} else {
			fmt.Printf("  agent pools deleted: %d\n", r.AgentPoolDeleted)
		}
	}
	if r.GPGKeyDeleted > 0 {
		if dryRun {
			fmt.Printf("  would delete:  %d gpg key(s)\n", r.GPGKeyDeleted)
		} else {
			fmt.Printf("  gpg keys deleted: %d\n", r.GPGKeyDeleted)
		}
	}
	if len(r.ConnectionsLeft) > 0 {
		fmt.Printf("  vcs connections left in place (operator-owned): %d\n", len(r.ConnectionsLeft))
	}
	if len(r.Errors) > 0 {
		fmt.Printf("  errors:        %d\n", len(r.Errors))
	}
	for _, g := range r.GPGKeys {
		fmt.Printf("    [%-16s] gpg-key %s", g.Action, g.KeyID)
		if g.TerrapodID != "" {
			fmt.Printf(" (%s)", g.TerrapodID)
		}
		fmt.Println()
		if g.Detail != "" {
			fmt.Printf("        - %s\n", g.Detail)
		}
	}
	for _, a := range r.AgentPools {
		fmt.Printf("    [%-16s] agent-pool %s", a.Action, a.Name)
		if a.TerrapodID != "" {
			fmt.Printf(" (%s)", a.TerrapodID)
		}
		fmt.Println()
		if a.Detail != "" {
			fmt.Printf("        - %s\n", a.Detail)
		}
	}
	for _, n := range r.Notifications {
		fmt.Printf("    [%-16s] notification %s / %s", n.Action, n.Workspace, n.Name)
		if n.TerrapodID != "" {
			fmt.Printf(" (%s)", n.TerrapodID)
		}
		fmt.Println()
		if n.Detail != "" {
			fmt.Printf("        - %s\n", n.Detail)
		}
	}
	for _, t := range r.RunTriggers {
		fmt.Printf("    [%-16s] run-trigger %s", t.Action, t.Pair)
		if t.TerrapodID != "" {
			fmt.Printf(" (%s)", t.TerrapodID)
		}
		fmt.Println()
		if t.Detail != "" {
			fmt.Printf("        - %s\n", t.Detail)
		}
	}
	for _, v := range r.VariableSets {
		fmt.Printf("    [%-16s] varset %s", v.Action, v.Name)
		if v.TerrapodID != "" {
			fmt.Printf(" (%s)", v.TerrapodID)
		}
		fmt.Println()
		if v.Detail != "" {
			fmt.Printf("        - %s\n", v.Detail)
		}
	}
	for _, w := range r.Workspaces {
		fmt.Printf("    [%-16s] %s", w.Action, w.SourceName)
		if w.TerrapodID != "" {
			fmt.Printf(" (%s)", w.TerrapodID)
		}
		fmt.Println()
		if w.Detail != "" {
			fmt.Printf("        - %s\n", w.Detail)
		}
	}
	if dryRun && r.DeletedCount > 0 {
		fmt.Println("\n  Re-run with --apply to delete the workspaces listed above.")
	}
}
