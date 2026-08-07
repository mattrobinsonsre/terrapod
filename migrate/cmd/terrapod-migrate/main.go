// Command terrapod-migrate migrates Terraform platform state into Terrapod
// from a supported source platform.
//
// Subcommands (will be wired in in subsequent increments):
//
//	apply      — read from the source, write to Terrapod (dry-run by default)
//	rewrite    — rewrite HCL `cloud {}` / `backend "remote"` / private module
//	             sources in an operator-supplied local directory tree. Does
//	             not interact with VCS — operator commits and pushes after.
//	verify     — confirm migrated workspaces match (state file, or live source)
//	rollback   — delete what a migration created (reversible migration)
//	status     — print the contents of the migration state file
//
// Migration is dry-run by default; pass --apply to actually write. Every
// run reads and writes a JSON state file (default: ./migration-state.json)
// so re-running is idempotent and the rewrite subcommand can pick up the
// source/destination host + per-workspace name mapping automatically.
package main

import (
	"context"
	"errors"
	"fmt"
	"os"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// init hands our build-time-pinned version to the SDK so its VersionCheck
// has something to compare against.
//
// Without this the gate is inert, not merely lenient: VersionCheck returns
// nil immediately when SDKVersion is "dev", SDKVersion defaults to "dev",
// and migrate's GoReleaser stamps main.Version — not the SDK's variable. So
// every released binary skipped the check entirely. terrapod-mcp already
// does the same assignment; this brings migrate in line (#1286).
//
// A source build leaves Version as "dev", which the SDK treats as "cannot
// compare" and skips — so `go run ./cmd/terrapod-migrate` keeps working
// without the override flag.
func init() {
	terrapod.SDKVersion = Version
}

// checkAPIVersion gates a run on tool↔API compatibility (#550).
//
// Returns a non-nil error when the run must be refused; the caller prints
// it and exits non-zero. `allow` is the --allow-api-version-mismatch
// escape hatch, which downgrades every refusal to a warning.
//
//	compatible, or a dev build   → run
//	ErrVersionMismatch           → refuse (warn when allowed)
//	ErrVersionUnreported         → refuse (warn when allowed)
//	probe failed (network/HTTP)  → warn, run — even when not allowed
//
// The last row is deliberate: a version probe that itself fails must not
// block a migration. That was the original rationale for warning on
// everything, and it is worth keeping for the case it was actually about
// — an unreachable discovery endpoint — without extending it to cover a
// mismatch the probe successfully reported.
//
// The refusal is strict by default because migrate writes workspaces,
// variables, state and registry tarballs. A shape mismatch mid-migration
// is expensive to unpick, and the failure mode is quiet: an API that does
// not know an attribute ignores it and still answers 200.
func checkAPIVersion(c *terrapod.Client, allow bool) error {
	err := c.VersionCheck(context.Background())
	if err == nil {
		return nil
	}

	blocking := errors.Is(err, terrapod.ErrVersionMismatch) ||
		errors.Is(err, terrapod.ErrVersionUnreported)
	if !blocking {
		// Probe failure. Say so, but do not stand in the way.
		fmt.Fprintf(os.Stderr, "warning: could not verify Terrapod API version: %v\n", err)
		return nil
	}

	if allow {
		fmt.Fprintf(os.Stderr, "warning: %v (proceeding: --allow-api-version-mismatch)\n", err)
		return nil
	}
	return fmt.Errorf("%w\n\n"+
		"terrapod-migrate writes workspaces, variables, state and registry data, so it\n"+
		"refuses to run against an API it was not built for. Use a matching release, or\n"+
		"pass --allow-api-version-mismatch to override deliberately", err)
}

// Version is the build-time-pinned tool version. It identifies the tool in
// the User-Agent and in the migration state file, and backs `--version`.
// Mutation is intentional: GoReleaser stamps the actual semver at release
// time via -ldflags="-X main.Version=...".
//
// It is also handed to the SDK (see init above) as the version its
// compatibility gate compares against, so a mismatch against the target
// API refuses the run unless --allow-api-version-mismatch is passed.
// See checkAPIVersion for the full policy.
var Version = "dev"

func main() {
	if len(os.Args) < 2 {
		printUsage()
		os.Exit(2)
	}
	rest := os.Args[2:]
	switch os.Args[1] {
	case "apply":
		os.Exit(applyCmd(rest))
	case "status":
		os.Exit(statusCmd(rest))
	case "rewrite":
		os.Exit(rewriteCmd(rest))
	case "verify":
		os.Exit(verifyCmd(rest))
	case "rollback":
		os.Exit(rollbackCmd(rest))
	case "cutover":
		os.Exit(cutoverCmd(rest))
	case "version", "-v", "--version":
		fmt.Println(Version)
	case "help", "-h", "--help":
		printUsage()
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand %q\n\n", os.Args[1])
		printUsage()
		os.Exit(2)
	}
}

func printUsage() {
	fmt.Fprintf(os.Stderr, `terrapod-migrate %s — migrate a Terraform platform onto Terrapod

USAGE:
  terrapod-migrate <subcommand> [flags]

SUBCOMMANDS:
  apply     Read from --source (tfe|atlantis), write to --target Terrapod.
            Default is dry-run; pass --apply to write. Migrates workspaces,
            variables, VCS connections, and state.
  rewrite   Mechanically rewrite HCL cloud{}/backend"remote"{}/private
            module sources in a local directory. No VCS interaction.
  verify    Read back the migrated workspaces from Terrapod and confirm
            they match the migration state file (or, with --source, diff
            against the live source platform). Exits non-zero on mismatch.
  rollback  Reverse a migration: delete the workspaces this migration
            created (recorded in the state file). Default is dry-run;
            pass --apply to delete. Never deletes pre-existing or
            already-used workspaces, nor operator-owned VCS connections.
  cutover   Generate the handover Markdown doc; optionally --lock or
            --unlock source workspaces (TFE only) during the cutover.
  status    Print the contents of the migration state file.

  version   Print the tool version.
  help      Print this message.

DOCUMENTATION:
  docs/migration.md in the Terrapod repo.
`, Version)
}
