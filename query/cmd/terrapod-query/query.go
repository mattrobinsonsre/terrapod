package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"

	"github.com/mattrobinsonsre/terrapod/query/internal/query"
	"github.com/mattrobinsonsre/terrapod/query/internal/tofu"
)

// stringSlice collects repeatable flags (--filter, --arg).
type stringSlice []string

func (s *stringSlice) String() string { return strings.Join(*s, ",") }
func (s *stringSlice) Set(v string) error {
	*s = append(*s, v)
	return nil
}

// runQuery executes a single data-source discovery query via tofu and prints
// the structured result as JSON. It is the D2 surface: given a data source and
// its narrowing arguments, run it and return what tofu read back. It performs
// no import — turning the result into import blocks is D3.
func runQuery(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("query", flag.ExitOnError)
	dsType := fs.String("type", "", "data-source type to query, e.g. aws_vpcs (required)")
	name := fs.String("name", "q", "local name for the data block")
	providerConfigFile := fs.String("provider-config", "", "path to a .tf file with the provider block(s) and required_providers (required)")
	dir := fs.String("dir", "", "working directory (default: a fresh temp dir, removed unless --keep)")
	bin := fs.String("tofu", "", `path to the tofu binary (default: "tofu" on PATH)`)
	keep := fs.Bool("keep", false, "keep the working directory instead of removing it")
	var filters stringSlice
	var argVals stringSlice
	fs.Var(&filters, "filter", "repeatable filter as NAME=VAL1,VAL2 (e.g. tag:env=prod)")
	fs.Var(&argVals, "arg", "repeatable top-level argument as NAME=HCL (e.g. owners=[\"self\"])")
	if err := fs.Parse(args); err != nil {
		return err
	}

	if *dsType == "" {
		return fmt.Errorf("--type is required")
	}
	if *providerConfigFile == "" {
		return fmt.Errorf("--provider-config is required (provider block + required_providers)")
	}
	providerConfig, err := os.ReadFile(*providerConfigFile)
	if err != nil {
		return fmt.Errorf("read provider config: %w", err)
	}

	q := query.Query{Type: *dsType, Name: *name, Args: map[string]string{}}
	for _, f := range filters {
		name, valsCSV, ok := strings.Cut(f, "=")
		if !ok {
			return fmt.Errorf("bad --filter %q, want NAME=VAL1,VAL2", f)
		}
		q.Filters = append(q.Filters, query.Filter{Name: name, Values: strings.Split(valsCSV, ",")})
	}
	for _, a := range argVals {
		name, hcl, ok := strings.Cut(a, "=")
		if !ok {
			return fmt.Errorf("bad --arg %q, want NAME=HCL", a)
		}
		q.Args[name] = hcl
	}

	// Resolve the working directory: caller-supplied or a fresh temp dir.
	workDir := *dir
	if workDir == "" {
		workDir, err = os.MkdirTemp("", "terrapod-query-*")
		if err != nil {
			return fmt.Errorf("create work dir: %w", err)
		}
		if !*keep {
			defer os.RemoveAll(workDir)
		}
	}

	exec := &query.Executor{Runner: &tofu.Runner{Bin: *bin, Dir: workDir}}
	res, err := exec.Run(ctx, string(providerConfig), q)
	if err != nil {
		return err
	}

	out, err := json.MarshalIndent(res, "", "  ")
	if err != nil {
		return err
	}
	fmt.Println(string(out))
	if *keep || *dir != "" {
		fmt.Fprintln(os.Stderr, "working directory:", workDir)
	}
	return nil
}
