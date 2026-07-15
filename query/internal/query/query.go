// Package query runs a single data-source discovery query via native OpenTofu
// and returns the structured result.
//
// This is deliverable D2 of #823. Given a data-source query (which data source,
// which narrowing arguments), it writes a tiny ephemeral configuration — the
// caller's provider block plus a `data` block and an `output` — runs tofu in a
// scratch directory, and reads the result back with `tofu output -json`. tofu
// executes the data source natively; we parse structured JSON, never scraped
// text.
//
// It is read-only: a discovery configuration contains only a `data` block and
// an `output`, so applying it issues provider read/`Describe` calls and makes
// no changes to infrastructure. Turning the result into `import {}` blocks is
// D3; deciding which query to run is #824.
package query

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/mattrobinsonsre/terrapod/query/internal/tofu"
)

// outputName is the fixed name of the output the discovery config exposes.
const outputName = "terrapod_query_result"

// Filter is one repeated `filter { name, values }` block — the canonical
// EC2-style narrowing mechanism many data sources accept.
type Filter struct {
	Name   string
	Values []string
}

// Query describes one data-source discovery query.
type Query struct {
	// Type is the data-source type, e.g. "aws_vpcs".
	Type string
	// Name is the local block name; defaults to "q" when empty.
	Name string
	// Filters are repeated `filter {}` blocks.
	Filters []Filter
	// Args are additional top-level arguments, name → raw HCL expression (e.g.
	// "owners" → `["self"]`, "most_recent" → `true`). The value is emitted
	// verbatim as HCL, so the caller controls type; this mirrors Terrapod's
	// existing hcl=true variable convention.
	Args map[string]string
}

// localName returns the block name, defaulting to "q".
func (q Query) localName() string {
	if q.Name != "" {
		return q.Name
	}
	return "q"
}

// Result is the parsed outcome of a discovery query.
type Result struct {
	// Type and Name echo the query's data source and local name.
	Type string `json:"type"`
	Name string `json:"name"`
	// Value is the raw JSON of the data source's attributes (the `output`
	// value). D3 derives import ids from this.
	Value json.RawMessage `json:"value"`
	// Empty is true when the query matched nothing (a plural/list source whose
	// `ids` came back empty).
	Empty bool `json:"empty"`
}

// RenderConfig returns the HCL for the discovery config's query portion — the
// `data` block plus the `output`. The provider configuration is supplied
// separately (RenderConfig does not know the provider); the executor writes
// both files. Exposed for testability.
func (q Query) RenderConfig() string {
	var b strings.Builder
	fmt.Fprintf(&b, "data %q %q {\n", q.Type, q.localName())
	for _, f := range q.Filters {
		b.WriteString("  filter {\n")
		fmt.Fprintf(&b, "    name   = %s\n", hclString(f.Name))
		b.WriteString("    values = [")
		for i, v := range f.Values {
			if i > 0 {
				b.WriteString(", ")
			}
			b.WriteString(hclString(v))
		}
		b.WriteString("]\n  }\n")
	}
	// Deterministic arg ordering for stable output and tests.
	names := make([]string, 0, len(q.Args))
	for name := range q.Args {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		fmt.Fprintf(&b, "  %s = %s\n", name, q.Args[name])
	}
	b.WriteString("}\n\n")
	fmt.Fprintf(&b, "output %q {\n  value = data.%s.%s\n}\n", outputName, q.Type, q.localName())
	return b.String()
}

// Executor runs queries by orchestrating tofu in a working directory.
type Executor struct {
	// Runner is the tofu orchestrator; its Dir is the ephemeral working dir the
	// executor writes configuration into.
	Runner *tofu.Runner
}

// Run writes the discovery configuration (the caller's providerConfig plus the
// query), initialises tofu, applies (read-only — data sources only), and reads
// the output back as structured JSON.
func (e *Executor) Run(ctx context.Context, providerConfig string, q Query) (*Result, error) {
	dir := e.Runner.Dir
	if err := os.WriteFile(filepath.Join(dir, "providers.tf"), []byte(providerConfig), 0o600); err != nil {
		return nil, fmt.Errorf("write providers.tf: %w", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "query.tf"), []byte(q.RenderConfig()), 0o600); err != nil {
		return nil, fmt.Errorf("write query.tf: %w", err)
	}

	if err := e.Runner.Init(ctx); err != nil {
		return nil, fmt.Errorf("init: %w", err)
	}
	if err := e.Runner.Apply(ctx); err != nil {
		// A data source that matches nothing (or too much) errors here; surface
		// it verbatim so #824 can react (e.g. loosen the filter and retry).
		return nil, fmt.Errorf("apply query: %w", err)
	}

	raw, err := e.Runner.OutputJSON(ctx, outputName)
	if err != nil {
		return nil, fmt.Errorf("read output: %w", err)
	}
	return parseResult(q, raw)
}

// parseResult builds a Result from the raw `output -json` value.
func parseResult(q Query, raw []byte) (*Result, error) {
	value := json.RawMessage(raw)
	// Validate it's JSON and inspect for the plural-empty case.
	var obj map[string]json.RawMessage
	empty := false
	if err := json.Unmarshal(raw, &obj); err == nil {
		if ids, ok := obj["ids"]; ok {
			var list []json.RawMessage
			if json.Unmarshal(ids, &list) == nil && len(list) == 0 {
				empty = true
			}
		}
	}
	return &Result{Type: q.Type, Name: q.localName(), Value: value, Empty: empty}, nil
}

// hclString renders s as a valid HCL double-quoted string literal. HCL string
// escaping matches Go's for the ASCII cases discovery filter names/values use
// (quotes, backslashes, control chars); strconv via %q is correct for these.
func hclString(s string) string {
	return fmt.Sprintf("%q", s)
}
