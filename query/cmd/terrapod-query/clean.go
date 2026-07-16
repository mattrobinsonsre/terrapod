package main

import (
	"context"
	"flag"
	"fmt"
	"os"

	"github.com/mattrobinsonsre/terrapod/query/internal/clean"
	"github.com/mattrobinsonsre/terrapod/query/internal/schema"
)

// runClean deterministically prunes a `tofu plan -generate-config-out` document
// so it plans import-only, using only the provider schema (no AI). See
// internal/clean for the rules. The provider schema is loaded exactly like the
// `schema` command: --from a captured document, or by shelling tofu in --dir.
func runClean(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("clean", flag.ExitOnError)
	config := fs.String("config", "", "path to the generate-config-out HCL to clean (required)")
	out := fs.String("out", "", "write the cleaned HCL here (default: stdout)")
	dir := fs.String("dir", ".", "working directory containing an initialised OpenTofu configuration (for schema)")
	bin := fs.String("tofu", "", `path to the tofu binary (default: "tofu" on PATH)`)
	from := fs.String("from", "", "read a captured `providers schema -json` document from this file instead of running tofu")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *config == "" {
		return fmt.Errorf("--config is required (the generate-config-out HCL to clean)")
	}

	src, err := os.ReadFile(*config)
	if err != nil {
		return fmt.Errorf("read config: %w", err)
	}
	raw, err := loadSchema(ctx, *from, *dir, *bin)
	if err != nil {
		return err
	}
	sch, err := schema.Parse(raw)
	if err != nil {
		return err
	}

	cleaned, rep, err := clean.Clean(src, sch)
	if err != nil {
		return err
	}

	if *out != "" {
		if err := os.WriteFile(*out, cleaned, 0o600); err != nil {
			return fmt.Errorf("write %s: %w", *out, err)
		}
		fmt.Fprintf(
			os.Stderr,
			"cleaned %d resource(s): removed %d computed-only + %d zero-valued attribute(s)\n",
			rep.Resources, rep.RemovedComputed, rep.RemovedZero,
		)
		return nil
	}
	fmt.Print(string(cleaned))
	return nil
}
