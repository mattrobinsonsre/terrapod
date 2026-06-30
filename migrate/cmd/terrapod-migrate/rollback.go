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
// migration state file and deletes the Terrapod workspaces THIS
// migration created — making a migration reversible, which is what makes
// it approvable: "we can undo it" removes the largest switching-cost
// objection.
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
	DryRun          bool                `json:"dry_run"`
	Target          string              `json:"target"`
	Workspaces      []RollbackWorkspace `json:"workspaces"`
	ConnectionsLeft []string            `json:"connections_left_in_place,omitempty"`
	DeletedCount    int                 `json:"deleted_count"`
	SkippedCount    int                 `json:"skipped_count"`
	Errors          []string            `json:"errors,omitempty"`
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
	if len(r.ConnectionsLeft) > 0 {
		fmt.Printf("  vcs connections left in place (operator-owned): %d\n", len(r.ConnectionsLeft))
	}
	if len(r.Errors) > 0 {
		fmt.Printf("  errors:        %d\n", len(r.Errors))
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
