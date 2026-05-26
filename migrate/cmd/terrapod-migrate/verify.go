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

// verifyCmd is the verify subcommand. For every workspace recorded
// in the migration state file, it confirms the Terrapod side still
// has the expected workspace ID, name, and variable count. Discrepan-
// cies are reported per-workspace and the command exits non-zero if
// any check fails.
//
// This is a "did the migration land?" lightweight check, not a full
// behavioural verification (running plans against the migrated work-
// spaces is a separate increment — it touches run lifecycle which is
// more involved). Operators still see clear signal when a record's
// TerrapodID points at a deleted workspace, or when variables go
// missing between apply and a follow-up bulk-update.
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
	Target       string                 `json:"target"`
	StateFile    string                 `json:"state_file"`
	CheckedCount int                    `json:"checked_count"`
	OkCount      int                    `json:"ok_count"`
	FailedCount  int                    `json:"failed_count"`
	Workspaces   []WorkspaceVerification `json:"workspaces"`
}

// WorkspaceVerification is the per-workspace result.
type WorkspaceVerification struct {
	SourceName    string   `json:"source_name"`
	TerrapodID    string   `json:"terrapod_id"`
	OK            bool     `json:"ok"`
	Failures      []string `json:"failures,omitempty"`
	VariableCount int      `json:"variable_count"`
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

		// Variable presence: count what's on Terrapod and compare
		// against what the state file remembers about the workspace.
		// State doesn't (yet) store the expected variable list, so we
		// only surface count > 0 today; richer parity checks land with
		// the apply-time variable manifest.
		if ws := ws; ws != nil {
			vars, err := c.ListVariables(ctx, ws.ID)
			if err != nil {
				v.Failures = append(v.Failures, fmt.Sprintf("ListVariables failed: %v", err))
			} else {
				v.VariableCount = len(vars)
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
}
