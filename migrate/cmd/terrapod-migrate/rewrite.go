package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"github.com/mattrobinsonsre/terrapod/migrate/internal/framework"
	"github.com/mattrobinsonsre/terrapod/migrate/internal/rewriter"
)

// rewriteCmd is the rewrite subcommand: mechanically rewrite HCL
// cloud{}/backend"remote"{} blocks in a local directory to point at
// Terrapod. Two modes:
//
//   * state-driven (default): read SourceHost/DestHost/workspace
//     mapping from the migration state file produced by `apply`. This
//     is what operators use after running apply against a real source.
//
//   * explicit-flags: pass --source-host / --dest-host directly. Used
//     for one-off rewrites without going through apply (e.g. an
//     operator who already migrated state by hand and just wants the
//     HCL pointed at Terrapod).
//
// Default is dry-run; pass --write to touch disk.
func rewriteCmd(args []string) int {
	fs := flag.NewFlagSet("rewrite", flag.ContinueOnError)
	var (
		dir        = fs.String("dir", "", "Local directory to rewrite (recurses into *.tf files; required)")
		statePath  = fs.String("state-file", framework.DefaultStateFile, "Migration state file (used for source/dest hosts)")
		sourceHost = fs.String("source-host", "", "Override: source platform hostname (otherwise read from --state-file)")
		destHost   = fs.String("dest-host", "", "Override: Terrapod hostname (otherwise read from --state-file)")
		sourceOrg  = fs.String("source-org", "", "Override: source TFE organisation (otherwise read from --state-file)")
		write      = fs.Bool("write", false, "Actually write changes to disk (default is dry-run)")
		jsonOut    = fs.Bool("json", false, "Emit the rewrite report as JSON")
	)
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *dir == "" {
		fmt.Fprintln(os.Stderr, "rewrite: --dir is required")
		return 2
	}

	var (
		report *rewriter.Report
		err    error
	)
	dryRun := !*write

	// Explicit-flags mode wins when both host overrides are present.
	// Half-supplied overrides (source-host only or dest-host only)
	// are an error: we never silently fall back to the state file for
	// the other half.
	switch {
	case *sourceHost != "" && *destHost != "":
		report, err = rewriter.RewriteDir(*dir, rewriter.Options{
			SourceHost: *sourceHost,
			DestHost:   *destHost,
			SourceOrg:  *sourceOrg,
			DryRun:     dryRun,
		})
	case *sourceHost != "" || *destHost != "":
		fmt.Fprintln(os.Stderr, "rewrite: --source-host and --dest-host must be provided together (or omit both to read from --state-file)")
		return 2
	default:
		state, lerr := framework.Load(*statePath)
		if lerr != nil {
			fmt.Fprintf(os.Stderr, "rewrite: load state file %s: %v\n", *statePath, lerr)
			return 1
		}
		if state == nil {
			fmt.Fprintf(os.Stderr, "rewrite: state file %s not found — pass --source-host / --dest-host to rewrite without one\n", *statePath)
			return 1
		}
		report, err = rewriter.RewriteFromState(*dir, state, dryRun)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "rewrite: %v\n", err)
		return 1
	}

	if *jsonOut {
		if data, jerr := json.MarshalIndent(report, "", "  "); jerr == nil {
			fmt.Println(string(data))
		}
	} else {
		printRewriteSummary(report, dryRun)
	}
	return 0
}

func printRewriteSummary(r *rewriter.Report, dryRun bool) {
	verb := "would rewrite"
	if !dryRun {
		verb = "rewrote"
	}
	fmt.Printf("\nterrapod-migrate rewrite — %s %d file(s), %d unmodified\n", verb, r.Modified, r.Skipped)
	fmt.Printf("  root: %s\n\n", r.Root)
	for _, f := range r.Files {
		if !f.Modified && len(f.Notes) == 0 {
			continue
		}
		marker := " "
		if f.Modified {
			marker = "*"
		}
		fmt.Printf("  %s %s\n", marker, f.Path)
		for _, e := range f.Edits {
			fmt.Printf("        + %s\n", e)
		}
		for _, n := range f.Notes {
			fmt.Printf("        ! %s\n", n)
		}
	}
	if dryRun {
		fmt.Println("\n  (dry-run; pass --write to apply)")
	}
}
