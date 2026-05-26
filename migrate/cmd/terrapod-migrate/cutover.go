package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"github.com/mattrobinsonsre/terrapod/migrate/internal/framework"
	"github.com/mattrobinsonsre/terrapod/migrate/internal/sources/tfe"
)

// cutoverCmd is the cutover subcommand. Two operations on the source
// side (currently TFE-only — Atlantis has no notion of "lock the
// platform"):
//
//   --lock   lock every source workspace recorded in the state file
//   --unlock the reverse — for rollback
//
// Cutover also generates the operator handover doc — a Markdown
// runbook next to the state file — every time it's invoked (with or
// without --lock), so operators can pull a fresh doc after each
// apply iteration.
func cutoverCmd(args []string) int {
	fs := flag.NewFlagSet("cutover", flag.ContinueOnError)
	var (
		statePath  = fs.String("state-file", framework.DefaultStateFile, "Path to the migration state JSON file")
		lock       = fs.Bool("lock", false, "Lock every recorded source workspace (TFE only)")
		unlock     = fs.Bool("unlock", false, "Unlock every recorded source workspace (TFE only)")
		tfeAddress = fs.String("tfe-address", os.Getenv("TFE_ADDRESS"), "TFE API address (or TFE_ADDRESS)")
		tfeToken   = fs.String("tfe-token", os.Getenv("TFE_TOKEN"), "TFE API token (or TFE_TOKEN)")
		tfeOrg     = fs.String("tfe-org", os.Getenv("TFE_ORG"), "TFE organisation (or TFE_ORG)")
		reason     = fs.String("reason", "", "Lock reason (default: a generic 'locked by terrapod-migrate' message)")
		handoverTo = fs.String("write-handover", "", "Write the handover Markdown doc to this path (default: alongside the state file as MIGRATION-HANDOVER.md)")
	)
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *lock && *unlock {
		fmt.Fprintln(os.Stderr, "cutover: --lock and --unlock are mutually exclusive")
		return 2
	}

	state, err := framework.Load(*statePath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "cutover: load state file %s: %v\n", *statePath, err)
		return 1
	}
	if state == nil {
		fmt.Fprintf(os.Stderr, "cutover: state file %s not found (run `apply` first)\n", *statePath)
		return 1
	}

	// Always produce a fresh handover doc, even when --lock/--unlock
	// aren't set — operators run cutover repeatedly during a phased
	// migration and a current handover is the most useful artifact.
	handoverPath := *handoverTo
	if handoverPath == "" {
		handoverPath = filepath.Join(filepath.Dir(*statePath), "MIGRATION-HANDOVER.md")
	}
	if err := os.WriteFile(handoverPath, framework.RenderHandoverMarkdown(state), 0o644); err != nil {
		fmt.Fprintf(os.Stderr, "cutover: write handover doc %s: %v\n", handoverPath, err)
		return 1
	}
	fmt.Printf("Wrote handover doc to %s\n", handoverPath)

	// Lock/unlock only run when explicitly requested and only on
	// TFE-sourced migrations.
	if !*lock && !*unlock {
		return 0
	}
	if state.Source != "tfe" {
		fmt.Fprintf(os.Stderr, "cutover: --lock/--unlock are TFE-only; state file recorded source=%q\n", state.Source)
		return 1
	}
	if *tfeToken == "" || *tfeOrg == "" {
		fmt.Fprintln(os.Stderr, "cutover: --tfe-token and --tfe-org are required for --lock/--unlock")
		return 2
	}

	c, err := tfe.NewClient(context.Background(), tfe.Config{
		Address: *tfeAddress,
		Token:   *tfeToken,
		OrgName: *tfeOrg,
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "cutover: build tfe client: %v\n", err)
		return 1
	}

	ids := make([]string, 0, len(state.Workspaces))
	for _, ws := range state.Workspaces {
		ids = append(ids, ws.SourceID)
	}

	var (
		count int
		errs  []error
	)
	if *lock {
		count, errs = c.LockWorkspaces(context.Background(), ids, *reason)
		fmt.Printf("Locked %d/%d source workspaces.\n", count, len(ids))
	} else {
		count, errs = c.UnlockWorkspaces(context.Background(), ids)
		fmt.Printf("Unlocked %d/%d source workspaces.\n", count, len(ids))
	}
	for _, e := range errs {
		fmt.Fprintf(os.Stderr, "  - %v\n", e)
	}
	if len(errs) > 0 {
		return 1
	}
	return 0
}
