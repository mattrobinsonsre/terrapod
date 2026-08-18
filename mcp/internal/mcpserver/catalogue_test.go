package mcpserver

import (
	"context"
	"encoding/json"
	"os"
	"sort"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// catalogueGolden is the committed snapshot of the MCP tool catalogue — every
// tool's name + input schema. Once agents depend on tool names and schemas,
// removing/renaming/retyping a tool is a BREAKING change (the MCP analogue of
// dropping an /api/v2 route), so this test freezes the catalogue and fails on
// any non-additive change. Adding a tool (or an optional field) is additive —
// regenerate consciously with:
//
//	UPDATE_MCP_CATALOGUE=1 go test ./internal/mcpserver -run TestToolCatalogue
//
// Part of the #550 no-breaking-changes program.
const catalogueGolden = "tool_catalogue.json"

// toolEntry is one catalogued tool: its name, whether it's read-only or
// destructive (annotations agents/hosts key confirmations off), and its input
// schema (the wire contract).
type toolEntry struct {
	Name        string          `json:"name"`
	ReadOnly    bool            `json:"read_only,omitempty"`
	Destructive bool            `json:"destructive,omitempty"`
	InputSchema json.RawMessage `json:"input_schema"`
}

// liveCatalogue connects an in-memory client to a freshly-built server and
// returns the registered tools as a stable, sorted catalogue.
func liveCatalogue(t *testing.T) []toolEntry {
	t.Helper()
	srv, _, err := New(Config{Host: "example.test", Name: "terrapod-test", Token: "test-token"})
	if err != nil {
		t.Fatalf("build server: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	t.Cleanup(cancel)

	clientT, serverT := mcp.NewInMemoryTransports()
	go func() { _ = srv.Run(ctx, serverT) }()

	client := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "0"}, nil)
	sess, err := client.Connect(ctx, clientT, nil)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(func() { _ = sess.Close() })

	res, err := sess.ListTools(ctx, nil)
	if err != nil {
		t.Fatalf("list tools: %v", err)
	}
	out := make([]toolEntry, 0, len(res.Tools))
	for _, tool := range res.Tools {
		schema, _ := json.Marshal(tool.InputSchema)
		e := toolEntry{Name: tool.Name, InputSchema: schema}
		if tool.Annotations != nil {
			e.ReadOnly = tool.Annotations.ReadOnlyHint
			e.Destructive = tool.Annotations.DestructiveHint != nil && *tool.Annotations.DestructiveHint
		}
		out = append(out, e)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

func TestToolCatalogue(t *testing.T) {
	cat := liveCatalogue(t)
	got, err := json.MarshalIndent(cat, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	got = append(got, '\n')

	if os.Getenv("UPDATE_MCP_CATALOGUE") != "" {
		if err := os.WriteFile(catalogueGolden, got, 0o644); err != nil {
			t.Fatal(err)
		}
		t.Logf("wrote %s (%d tools)", catalogueGolden, len(cat))
		return
	}

	want, err := os.ReadFile(catalogueGolden)
	if err != nil {
		t.Fatalf("read %s (regenerate with UPDATE_MCP_CATALOGUE=1): %v", catalogueGolden, err)
	}
	if string(got) != string(want) {
		t.Errorf("tool catalogue changed vs %s.\n"+
			"If you ONLY added tools/optional fields, this is additive — regenerate with:\n"+
			"  UPDATE_MCP_CATALOGUE=1 go test ./internal/mcpserver -run TestToolCatalogue\n"+
			"If you REMOVED/RENAMED/RETYPED a tool or a field, that is a BREAKING change "+
			"(agents depend on the catalogue) — bump MAJOR or restore it.\n\ngot:\n%s", catalogueGolden, got)
	}
}

// TestExpectedToolsPresent is a fast guard that the core tools exist and carry
// the right safety annotation — a removed tool or a read/write mislabel fails
// loudly independent of the full snapshot diff.
func TestExpectedToolsPresent(t *testing.T) {
	byName := map[string]toolEntry{}
	for _, e := range liveCatalogue(t) {
		byName[e.Name] = e
	}
	readOnlyTools := []string{
		"terrapod_workspace_list", "terrapod_workspace_get",
		"terrapod_run_list", "terrapod_run_get", "terrapod_run_plan_json",
		"terrapod_run_logs",
	}
	for _, n := range readOnlyTools {
		e, ok := byName[n]
		if !ok {
			t.Errorf("missing tool %q", n)
			continue
		}
		if !e.ReadOnly {
			t.Errorf("tool %q should be read-only", n)
		}
	}
	// The infra-changing tools must be flagged destructive so hosts confirm.
	for _, n := range []string{"terrapod_run_create", "terrapod_run_apply"} {
		e, ok := byName[n]
		if !ok {
			t.Errorf("missing tool %q", n)
			continue
		}
		if !e.Destructive {
			t.Errorf("tool %q should be marked destructive", n)
		}
	}
}
