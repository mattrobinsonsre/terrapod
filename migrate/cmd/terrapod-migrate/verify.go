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

// verifyCmd is the verify subcommand. For every workspace recorded in
// the migration state file it confirms the Terrapod side still has the
// expected workspace ID, name, variable count, and state serial/lineage;
// then it existence-checks the other migration-created resources
// (variable sets, run triggers, notifications, agent pools, GPG keys) —
// a NotFound means the resource was deleted post-migration. Discrepancies
// are reported per-item and the command exits non-zero if any check fails.
//
// This is a "did the migration land, and is it still there?" check, not a
// full behavioural verification (running plans against the migrated
// workspaces is a separate increment — it touches run lifecycle which is
// more involved). Operators still see clear signal when a record's
// TerrapodID points at a deleted resource, or when variables go missing
// between apply and a follow-up bulk-update.
func verifyCmd(args []string) int {
	fs := flag.NewFlagSet("verify", flag.ContinueOnError)
	var (
		target    = fs.String("target", os.Getenv("TERRAPOD_HOSTNAME"), "Terrapod base URL (or TERRAPOD_HOSTNAME)")
		token     = fs.String("token", os.Getenv("TERRAPOD_TOKEN"), "Terrapod API token (or TERRAPOD_TOKEN)")
		statePath = fs.String("state-file", framework.DefaultStateFile, "Path to the migration state JSON file")
		jsonOut   = fs.Bool("json", false, "Emit the verification report as JSON")
		skipTLS   = fs.Bool("skip-tls-verify", false, "Skip TLS certificate verification (dev only)")
	)
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *target == "" {
		fmt.Fprintln(os.Stderr, "verify: --target (or TERRAPOD_HOSTNAME) is required")
		return 2
	}
	if *token == "" {
		fmt.Fprintln(os.Stderr, "verify: --token (or TERRAPOD_TOKEN) is required")
		return 2
	}

	state, err := framework.Load(*statePath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "verify: load state file %s: %v\n", *statePath, err)
		return 1
	}
	if state == nil {
		fmt.Fprintf(os.Stderr, "verify: state file %s not found — run `apply` first\n", *statePath)
		return 1
	}

	c, err := terrapod.NewClient(terrapod.Options{
		BaseURL:       *target,
		Token:         *token,
		SkipTLSVerify: *skipTLS,
		UserAgent:     "terrapod-migrate/" + Version,
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "verify: build terrapod client: %v\n", err)
		return 1
	}

	report := runVerify(context.Background(), c, state)

	if *jsonOut {
		if data, err := json.MarshalIndent(report, "", "  "); err == nil {
			fmt.Println(string(data))
		}
	} else {
		printVerifySummary(report)
	}
	if report.FailedCount > 0 {
		return 1
	}
	return 0
}

// VerifyReport is the structured output of verifyCmd. Mirrors the
// writer.Report shape so the two are easy to diff in operator
// dashboards.
type VerifyReport struct {
	Target       string                  `json:"target"`
	StateFile    string                  `json:"state_file"`
	CheckedCount int                     `json:"checked_count"`
	OkCount      int                     `json:"ok_count"`
	FailedCount  int                     `json:"failed_count"`
	Workspaces   []WorkspaceVerification `json:"workspaces"`
	// Resources holds the non-workspace migration-created resources
	// (variable sets, run triggers, notifications, agent pools, GPG
	// keys) — an existence check that each still exists on Terrapod.
	Resources []ResourceVerification `json:"resources,omitempty"`
}

// WorkspaceVerification is the per-workspace result.
type WorkspaceVerification struct {
	SourceName            string   `json:"source_name"`
	TerrapodID            string   `json:"terrapod_id"`
	OK                    bool     `json:"ok"`
	Failures              []string `json:"failures,omitempty"`
	VariableCount         int      `json:"variable_count"`
	ExpectedVariableCount int      `json:"expected_variable_count,omitempty"`
}

// ResourceVerification is the per-resource existence-check result for the
// non-workspace resources the migration created.
type ResourceVerification struct {
	Kind       string   `json:"kind"`
	Name       string   `json:"name"`
	TerrapodID string   `json:"terrapod_id"`
	OK         bool     `json:"ok"`
	Failures   []string `json:"failures,omitempty"`
}

// checkResource GETs a migration-created resource and records whether it
// still exists on Terrapod. A NotFound means it was deleted post-migration.
func (r *VerifyReport) checkResource(kind, name, id string, get func() error) {
	rv := ResourceVerification{Kind: kind, Name: name, TerrapodID: id}
	if err := get(); err != nil {
		if terrapod.IsNotFound(err) {
			rv.Failures = append(rv.Failures, fmt.Sprintf("%s not found on Terrapod (deleted post-migration?)", kind))
		} else {
			rv.Failures = append(rv.Failures, fmt.Sprintf("Terrapod lookup failed: %v", err))
		}
	}
	rv.OK = len(rv.Failures) == 0
	r.Resources = append(r.Resources, rv)
	r.CheckedCount++
	if rv.OK {
		r.OkCount++
	} else {
		r.FailedCount++
	}
}

func runVerify(ctx context.Context, c *terrapod.Client, state *framework.State) *VerifyReport {
	report := &VerifyReport{Target: state.DestHost}
	for _, rec := range state.Workspaces {
		v := WorkspaceVerification{
			SourceName: rec.SourceName,
			TerrapodID: rec.TerrapodID,
		}
		if rec.TerrapodID == "" {
			v.Failures = append(v.Failures, "no terrapod_id in state — apply didn't create this workspace")
			report.Workspaces = append(report.Workspaces, v)
			report.CheckedCount++
			report.FailedCount++
			continue
		}

		ws, err := c.GetWorkspace(ctx, rec.TerrapodID)
		if err != nil {
			var nf *terrapod.NotFoundError
			if errors.As(err, &nf) {
				v.Failures = append(v.Failures, "workspace not found on Terrapod (was it deleted post-migration?)")
			} else {
				v.Failures = append(v.Failures, fmt.Sprintf("Terrapod GetWorkspace failed: %v", err))
			}
		} else {
			if ws.Name != rec.SourceName {
				// Renames are tolerated as long as the operator
				// documented them in the state file's labels; surface
				// the mismatch so accidental drift is visible.
				v.Failures = append(v.Failures, fmt.Sprintf("workspace name %q != state record source_name %q", ws.Name, rec.SourceName))
			}
		}

		// Variable parity: count what's on Terrapod and compare against
		// the count the migration recorded (ExpectedVarCount). A drop
		// between apply and now (a var deleted, or a half-applied run)
		// surfaces as a failure. Older state files that predate the
		// recorded count (ExpectedVarCount == 0) fall back to the
		// presence-only behaviour.
		if ws := ws; ws != nil {
			vars, err := c.ListVariables(ctx, ws.ID)
			if err != nil {
				v.Failures = append(v.Failures, fmt.Sprintf("ListVariables failed: %v", err))
			} else {
				v.VariableCount = len(vars)
				v.ExpectedVariableCount = rec.ExpectedVarCount
				if rec.ExpectedVarCount > 0 && len(vars) != rec.ExpectedVarCount {
					v.Failures = append(v.Failures, fmt.Sprintf(
						"variable count %d != migrated count %d (a variable was removed or never landed)",
						len(vars), rec.ExpectedVarCount))
				}
			}

			// State parity: the destination's current state serial/lineage
			// must still match what the migration uploaded. A lineage
			// mismatch means the workspace points at an unrelated state; a
			// serial below the migrated one means a rollback happened.
			if rec.StateLineage != "" {
				dest, serr := c.GetCurrentStateVersion(ctx, ws.ID)
				switch {
				case serr == nil && dest != nil:
					if dest.Lineage != rec.StateLineage {
						v.Failures = append(v.Failures, fmt.Sprintf(
							"state lineage %q != migrated lineage %q", dest.Lineage, rec.StateLineage))
					}
					if dest.Serial < rec.StateSerial {
						v.Failures = append(v.Failures, fmt.Sprintf(
							"state serial %d < migrated serial %d (state rolled back?)", dest.Serial, rec.StateSerial))
					}
				case terrapod.IsNotFound(serr):
					v.Failures = append(v.Failures, fmt.Sprintf(
						"migrated state (serial %d) is missing from the destination", rec.StateSerial))
				case serr != nil:
					v.Failures = append(v.Failures, fmt.Sprintf("GetCurrentStateVersion failed: %v", serr))
				}
			}
		}

		v.OK = len(v.Failures) == 0
		report.Workspaces = append(report.Workspaces, v)
		report.CheckedCount++
		if v.OK {
			report.OkCount++
		} else {
			report.FailedCount++
		}
	}

	// Non-workspace migration-created resources: an existence check that
	// each still exists on Terrapod (a NotFound means it was deleted
	// post-migration). Only resources this migration positively created
	// (CreatedByMigration + a recorded TerrapodID, not rolled back) are
	// checked — the same provenance gate rollback uses.
	for _, rec := range state.VarsetRollbackTargets() {
		report.checkResource("variable-set", rec.Name, rec.TerrapodID, func() error {
			_, err := c.GetVariableSet(ctx, rec.TerrapodID)
			return err
		})
	}
	for _, rec := range state.RunTriggerRollbackTargets() {
		report.checkResource("run-trigger", fmt.Sprintf("%s→%s", rec.SourceWorkspaceRef, rec.DestinationWorkspaceRef), rec.TerrapodID, func() error {
			_, err := c.GetRunTrigger(ctx, rec.TerrapodID)
			return err
		})
	}
	for _, rec := range state.NotificationRollbackTargets() {
		report.checkResource("notification", fmt.Sprintf("%s/%s", rec.WorkspaceRef, rec.Name), rec.TerrapodID, func() error {
			_, err := c.GetNotificationConfiguration(ctx, rec.TerrapodID)
			return err
		})
	}
	for _, rec := range state.AgentPoolRollbackTargets() {
		report.checkResource("agent-pool", rec.Name, rec.TerrapodID, func() error {
			_, err := c.GetAgentPool(ctx, rec.TerrapodID)
			return err
		})
	}
	for _, rec := range state.GPGKeyRollbackTargets() {
		report.checkResource("gpg-key", rec.KeyID, rec.TerrapodID, func() error {
			_, err := c.GetGPGKey(ctx, rec.TerrapodID)
			return err
		})
	}
	return report
}

func printVerifySummary(r *VerifyReport) {
	fmt.Printf("\nterrapod-migrate verify — %d checked, %d ok, %d failed\n", r.CheckedCount, r.OkCount, r.FailedCount)
	fmt.Printf("  target: %s\n\n", r.Target)
	for _, w := range r.Workspaces {
		marker := "ok "
		if !w.OK {
			marker = "FAIL"
		}
		fmt.Printf("  [%s] %s (terrapod_id=%s, vars=%d)\n", marker, w.SourceName, w.TerrapodID, w.VariableCount)
		for _, f := range w.Failures {
			fmt.Printf("        - %s\n", f)
		}
	}
	for _, rv := range r.Resources {
		marker := "ok "
		if !rv.OK {
			marker = "FAIL"
		}
		fmt.Printf("  [%s] %s %s (terrapod_id=%s)\n", marker, rv.Kind, rv.Name, rv.TerrapodID)
		for _, f := range rv.Failures {
			fmt.Printf("        - %s\n", f)
		}
	}
}
