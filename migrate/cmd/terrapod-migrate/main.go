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
	"fmt"
	"os"
)

// Version is the build-time-pinned tool version. It MUST match the target
// Terrapod API's reported version on startup (compared via
// /.well-known/terraform.json); mismatch refuses to run unless the operator
// passes --allow-api-version-mismatch. Mutation is intentional: GoReleaser
// stamps the actual semver at release time via -ldflags="-X main.Version=...".
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
