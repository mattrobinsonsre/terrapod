package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"github.com/mattrobinsonsre/terrapod/query/internal/schema"
	"github.com/mattrobinsonsre/terrapod/query/internal/tofu"
)

// runSchema introspects the provider schema and prints the discovery surface as
// JSON. By default it shells `tofu providers schema -json` in an initialised
// working directory; --from lets a caller feed an already-captured schema
// document (the API/runner may pass one it already has, and it makes the
// command testable without a live tofu).
func runSchema(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("schema", flag.ExitOnError)
	dir := fs.String("dir", ".", "working directory containing an initialised OpenTofu configuration")
	bin := fs.String("tofu", "", `path to the tofu binary (default: "tofu" on PATH)`)
	from := fs.String("from", "", "read a captured `providers schema -json` document from this file instead of running tofu")
	all := fs.Bool("all", false, "list every data source, not just the strong-signal discovery subset")
	importable := fs.Bool("importable", false, "list only data sources the deterministic import path can consume (a computed `ids` list) — the onboarding surface")
	if err := fs.Parse(args); err != nil {
		return err
	}

	raw, err := loadSchema(ctx, *from, *dir, *bin)
	if err != nil {
		return err
	}

	s, err := schema.Parse(raw)
	if err != nil {
		return err
	}

	candidates := s.QueryableDataSources()
	switch {
	case *all:
		candidates = s.Catalogue()
	case *importable:
		candidates = s.ImportableDataSources()
	}

	out, err := json.MarshalIndent(struct {
		Count       int                 `json:"count"`
		DataSources []schema.DataSource `json:"data_sources"`
	}{Count: len(candidates), DataSources: candidates}, "", "  ")
	if err != nil {
		return err
	}
	fmt.Println(string(out))
	return nil
}

// loadSchema returns the raw schema JSON, either from a file or by running tofu.
func loadSchema(ctx context.Context, from, dir, bin string) ([]byte, error) {
	if from != "" {
		return os.ReadFile(from)
	}
	r := &tofu.Runner{Bin: bin, Dir: dir}
	return r.ProvidersSchema(ctx)
}
