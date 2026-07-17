package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"

	"github.com/mattrobinsonsre/terrapod/query/discovery/importblock"
	"github.com/mattrobinsonsre/terrapod/query/discovery/query"
)

// runImport turns a query result (the JSON printed by `terrapod-query query`)
// into candidate `import {}` blocks. It is composable — pipe a query result in,
// get import blocks out:
//
//	terrapod-query query --type aws_vpcs ... | terrapod-query import --resource aws_vpc
//
// It performs no import; the emitted blocks are reviewed and, in #824, fed to
// `tofu plan -generate-config-out` behind the import-only plan gate.
func runImport(_ context.Context, args []string) error {
	fs := flag.NewFlagSet("import", flag.ExitOnError)
	from := fs.String("from", "", "read the query result JSON from this file (default: stdin)")
	resource := fs.String("resource", "", "managed resource type to import into (default: derived from the data source name)")
	out := fs.String("out", "", "write the import blocks to this file (default: stdout)")
	if err := fs.Parse(args); err != nil {
		return err
	}

	raw, err := readInput(*from)
	if err != nil {
		return err
	}
	var res query.Result
	if err := json.Unmarshal(raw, &res); err != nil {
		return fmt.Errorf("parse query result: %w", err)
	}

	blocks, err := importblock.FromResult(&res, importblock.Options{ResourceType: *resource})
	if err != nil {
		return err
	}
	importblock.SortBlocks(blocks)

	hcl := importblock.Render(blocks)
	if *out != "" {
		if err := os.WriteFile(*out, []byte(hcl), 0o600); err != nil {
			return fmt.Errorf("write %s: %w", *out, err)
		}
		fmt.Fprintf(os.Stderr, "wrote %d import block(s) to %s\n", len(blocks), *out)
		return nil
	}
	fmt.Print(hcl)
	if len(blocks) == 0 {
		fmt.Fprintln(os.Stderr, "no resources found; no import blocks emitted")
	}
	return nil
}

// readInput reads from a file, or stdin when path is empty.
func readInput(path string) ([]byte, error) {
	if path != "" {
		return os.ReadFile(path)
	}
	return io.ReadAll(os.Stdin)
}
