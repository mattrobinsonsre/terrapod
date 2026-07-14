// Command terrapod-query is the discovery engine for onboarding existing,
// unmanaged cloud resources into OpenTofu (#823). It is modelled after
// Terraform's `terraform query` — but built entirely on native OpenTofu
// functionality (schema introspection, data-source execution, and
// `-generate-config-out`), with no BUSL-licensed Terraform binary and no
// bespoke provider-plugin client.
//
// It runs as a stateless CLI: primary home is the runner discovery mode (baked
// into the runner image), and it is also published standalone for local use.
// It emits candidate `import {}` blocks and never performs the import itself —
// the actual import is a normal, gated Terrapod run (see #824).
package main

import (
	"context"
	"fmt"
	"os"
)

const usage = `terrapod-query — tofu-native discovery for onboarding existing resources

Usage:
  terrapod-query <command> [flags]

Commands:
  schema    Introspect the provider schema and print the discovery surface
            (the data sources usable for filter-based discovery and the
            arguments each accepts to narrow the search).
  query     Run one data-source query via tofu and print the structured
            result (the resources it found). Read-only; performs no import.

Run "terrapod-query <command> -h" for command-specific flags.
`

func main() {
	if len(os.Args) < 2 {
		fmt.Fprint(os.Stderr, usage)
		os.Exit(2)
	}

	ctx := context.Background()
	cmd, args := os.Args[1], os.Args[2:]

	var err error
	switch cmd {
	case "schema":
		err = runSchema(ctx, args)
	case "query":
		err = runQuery(ctx, args)
	case "-h", "--help", "help":
		fmt.Print(usage)
		return
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q\n\n%s", cmd, usage)
		os.Exit(2)
	}

	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}
