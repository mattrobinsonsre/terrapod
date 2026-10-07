package mcpserver

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// The declared-inventory tools (#1967, #1968) driven end to end: a real MCP
// client against a real server, backed by a fake Terrapod. The catalogue golden
// pins registration and TestEveryToolIsAnnotated pins the annotations; these
// pin that each tool reaches the right endpoint and surfaces the fields an
// agent has to reason over.

// inventoryToolCaller mirrors toolCaller but registers the inventory tools.
//
// It has its own helper rather than extending the shared one because the shared
// one is a fixture for a different group and widening it would make every one
// of those tests depend on this registration.
func inventoryToolCaller(t *testing.T, handler http.HandlerFunc) *mcp.ClientSession {
	t.Helper()
	api := httptest.NewServer(handler)
	t.Cleanup(api.Close)

	c, err := terrapod.NewClient(terrapod.Options{BaseURL: api.URL, Token: "t"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}

	srv := mcp.NewServer(&mcp.Implementation{Name: "test", Version: "0"}, nil)
	registerInventory(srv, c)

	ct, st := mcp.NewInMemoryTransports()
	ctx := context.Background()
	if _, err := srv.Connect(ctx, st, nil); err != nil {
		t.Fatalf("server connect: %v", err)
	}
	sess, err := mcp.NewClient(&mcp.Implementation{Name: "test-client", Version: "0"}, nil).
		Connect(ctx, ct, nil)
	if err != nil {
		t.Fatalf("client connect: %v", err)
	}
	t.Cleanup(func() { _ = sess.Close() })
	return sess
}

func callInventoryTool(
	t *testing.T, sess *mcp.ClientSession, name string, args map[string]any,
) *mcp.CallToolResult {
	t.Helper()
	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{Name: name, Arguments: args})
	if err != nil {
		t.Fatalf("CallTool %s: %v", name, err)
	}
	return res
}

func TestInventoryListToolPagesThroughTheDeclaredHosts(t *testing.T) {
	var paths []string
	sess := inventoryToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.URL.Path)
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(`{"data":[{"id":"invitem-1","type":"inventory-items","attributes":{
		  "name":"web-1","address":"10.0.0.4","groups":["web","eu"],
		  "vars":{"ansible_user":"deploy"},"created-at":"2026-10-07T09:00:00Z"},
		  "relationships":{"workspace":{"data":{"id":"ws-1","type":"workspaces"}}}}],
		  "meta":{"pagination":{"current-page":1,"page-size":100,"total-pages":1,"total-count":1}}}`))
	})

	res := callInventoryTool(t, sess, "terrapod_inventory_list", map[string]any{"workspace_id": "ws-1"})
	if res.IsError {
		t.Fatalf("tool reported an error: %s", resultText(t, res))
	}
	if len(paths) != 1 || paths[0] != "/api/v1/workspaces/ws-1/inventory-items" {
		t.Fatalf("paths = %v", paths)
	}
	out := resultText(t, res)
	// The agent needs the address and the groups, not just the name: they are
	// what decide whether a limit pattern will select the host.
	for _, want := range []string{`"count":1`, `"web-1"`, `"10.0.0.4"`, `"eu"`, `"ansible_user"`} {
		if !strings.Contains(out, want) {
			t.Fatalf("result missing %s: %s", want, out)
		}
	}
}

func TestInventoryListToolReportsAnEmptyDeclaredSet(t *testing.T) {
	sess := inventoryToolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"data":[],"meta":{"pagination":{"current-page":1,
		  "page-size":100,"total-pages":1,"total-count":0}}}`))
	})
	res := callInventoryTool(t, sess, "terrapod_inventory_list", map[string]any{"workspace_id": "ws-1"})
	if res.IsError {
		t.Fatalf("an empty declared set is the common answer, not an error: %s", resultText(t, res))
	}
	// `null` here would fail the derived output schema and cost the agent the
	// whole call, for the most ordinary result there is.
	if out := resultText(t, res); !strings.Contains(out, `"items":[]`) {
		t.Fatalf("items should be an empty array, not null: %s", out)
	}
}

func TestWorkspaceInventoriesToolCarriesTheSourceOrdering(t *testing.T) {
	var gotPath string
	sess := inventoryToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		_, _ = w.Write([]byte(`{"data":[{"id":"inv-1","type":"inventories","attributes":{
		  "name":"default","api-resolvable":false,"sources":[
		    {"id":"invsrc-1","position":0,"kind":"terraform","api-resolvable":true},
		    {"id":"invsrc-2","position":1,"kind":"git","api-resolvable":false,
		     "config":{"path":"inventory/prod.yml"}}]},
		  "relationships":{"workspace":{"data":{"id":"ws-1","type":"workspaces"}}}}]}`))
	})

	res := callInventoryTool(t, sess, "terrapod_workspace_inventories",
		map[string]any{"workspace_id": "ws-1"})
	if res.IsError {
		t.Fatalf("tool reported an error: %s", resultText(t, res))
	}
	if gotPath != "/api/v1/workspaces/ws-1/inventories" {
		t.Fatalf("path = %s", gotPath)
	}

	var out struct {
		Count       int                  `json:"count"`
		Inventories []terrapod.Inventory `json:"inventories"`
	}
	if err := json.Unmarshal([]byte(resultText(t, res)), &out); err != nil {
		t.Fatalf("unmarshal result: %v (%s)", err, resultText(t, res))
	}
	if out.Count != 1 || len(out.Inventories) != 1 {
		t.Fatalf("out = %+v", out)
	}
	inv := out.Inventories[0]
	// `api-resolvable: false` is the field that explains why a snapshot may be
	// older than the declared hosts, so losing it would be the whole point.
	if inv.APIResolvable {
		t.Fatal("api-resolvable should have survived as false")
	}
	if len(inv.Sources) != 2 {
		t.Fatalf("sources = %+v", inv.Sources)
	}
	// Position decides which source wins a conflicting host variable, so it has
	// to arrive as the integer it is rather than being flattened.
	if inv.Sources[0].Position != 0 || inv.Sources[0].Kind != "terraform" {
		t.Fatalf("source 0 = %+v", inv.Sources[0])
	}
	if inv.Sources[1].Position != 1 || inv.Sources[1].Kind != "git" {
		t.Fatalf("source 1 = %+v", inv.Sources[1])
	}
}

func TestInventoryResolvedToolReturnsTheTargetSet(t *testing.T) {
	var gotPath string
	sess := inventoryToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		_, _ = w.Write([]byte(`{"data":{"id":"invver-9","type":"inventory-versions","attributes":{
		  "host-count":2,"group-count":1,"produced-by":"api","produced-by-ref":"",
		  "taken-at":"2026-10-07T10:00:00Z",
		  "hosts":{"web-1":{"ansible_host":"10.0.0.4"},"web-2":{}},
		  "groups":{"web":["web-1","web-2"]},
		  "ansible-inventory":{"web":{"hosts":["web-1","web-2"]},"_meta":{"hostvars":{}}}},
		  "relationships":{"inventory":{"data":{"id":"inv-1","type":"inventories"}}}}}`))
	})

	res := callInventoryTool(t, sess, "terrapod_inventory_resolved",
		map[string]any{"inventory_id": "inv-1"})
	if res.IsError {
		t.Fatalf("tool reported an error: %s", resultText(t, res))
	}
	if gotPath != "/api/v1/inventories/inv-1/resolved" {
		t.Fatalf("path = %s", gotPath)
	}
	out := resultText(t, res)
	// `web-2` has no variables. Ansible's own shape omits such a host from
	// _meta.hostvars entirely, so its presence here is the exhaustive-hosts
	// property the agent's enumeration depends on.
	for _, want := range []string{`"web-1"`, `"web-2"`, `"taken-at":"2026-10-07T10:00:00Z"`, `"produced-by":"api"`} {
		if !strings.Contains(out, want) {
			t.Fatalf("result missing %s: %s", want, out)
		}
	}
	// The lossy rendering is off unless asked for.
	if strings.Contains(out, "ansible-inventory") || strings.Contains(out, "hostvars") {
		t.Fatalf("ansible shape should be omitted by default: %s", out)
	}
}

func TestInventoryResolvedToolIncludesTheAnsibleShapeOnRequest(t *testing.T) {
	sess := inventoryToolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"data":{"id":"invver-9","type":"inventory-versions","attributes":{
		  "host-count":1,"group-count":1,"produced-by":"runner","taken-at":"2026-10-07T10:00:00Z",
		  "hosts":{"web-1":{}},"groups":{"web":["web-1"]},
		  "ansible-inventory":{"web":{"hosts":["web-1"]}}}}}`))
	})
	res := callInventoryTool(t, sess, "terrapod_inventory_resolved",
		map[string]any{"inventory_id": "inv-1", "include_ansible_shape": true})
	if res.IsError {
		t.Fatalf("tool reported an error: %s", resultText(t, res))
	}
	if out := resultText(t, res); !strings.Contains(out, "ansible-inventory") {
		t.Fatalf("ansible shape was asked for and is missing: %s", out)
	}
}

// A 409 is the actionable answer, not a failure to hide: it names the source
// kinds that need ansible and says a runner has to produce the first snapshot.
func TestInventoryResolvedToolRelaysTheConflictDetail(t *testing.T) {
	sess := inventoryToolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"errors":[{"status":"409","detail":
		  "This inventory has never been resolved and contains a source the API cannot resolve. A configure or a resolve operation in a runner has to produce the first snapshot."}]}`))
	})
	res := callInventoryTool(t, sess, "terrapod_inventory_resolved",
		map[string]any{"inventory_id": "inv-1"})
	if !res.IsError {
		t.Fatal("want a tool error for a 409")
	}
	msg := resultText(t, res)
	for _, want := range []string{"cannot resolve", "runner"} {
		if !strings.Contains(msg, want) {
			t.Fatalf("the conflict detail was swallowed, leaving %q", msg)
		}
	}
}

func TestInventoryVersionsToolReturnsTheHistory(t *testing.T) {
	var gotPath string
	sess := inventoryToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		_, _ = w.Write([]byte(`{"data":[
		  {"id":"invver-2","type":"inventory-versions","attributes":{"host-count":3,
		    "group-count":2,"produced-by":"runner","produced-by-ref":"run-77",
		    "taken-at":"2026-10-07T11:00:00Z"}},
		  {"id":"invver-1","type":"inventory-versions","attributes":{"host-count":2,
		    "group-count":1,"produced-by":"api","taken-at":"2026-10-07T10:00:00Z"}}]}`))
	})

	res := callInventoryTool(t, sess, "terrapod_inventory_versions",
		map[string]any{"inventory_id": "inv-1"})
	if res.IsError {
		t.Fatalf("tool reported an error: %s", resultText(t, res))
	}
	if gotPath != "/api/v1/inventories/inv-1/versions" {
		t.Fatalf("path = %s", gotPath)
	}
	out := resultText(t, res)
	// "has a runner ever resolved this" is the question this answers, so
	// produced-by and its ref are the fields that must survive.
	for _, want := range []string{`"count":2`, `"produced-by":"runner"`, `"run-77"`, `"produced-by":"api"`} {
		if !strings.Contains(out, want) {
			t.Fatalf("result missing %s: %s", want, out)
		}
	}
}

func TestInventoryLimitPreviewToolPostsThePatternAndReportsTheReach(t *testing.T) {
	var gotPath string
	var gotBody map[string]any
	sess := inventoryToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &gotBody)
		_, _ = w.Write([]byte(`{"data":{"id":"invlp-1","type":"inventory-limit-previews",
		  "attributes":{"limit":"web:!web-2","hosts":["web-1"],"host-count":1,
		  "of-host-count":12,"taken-at":"2026-10-07T10:00:00Z"}}}`))
	})

	res := callInventoryTool(t, sess, "terrapod_inventory_limit_preview",
		map[string]any{"inventory_id": "inv-1", "limit": "web:!web-2"})
	if res.IsError {
		t.Fatalf("tool reported an error: %s", resultText(t, res))
	}
	if gotPath != "/api/v1/inventories/inv-1/actions/preview-limit" {
		t.Fatalf("path = %s", gotPath)
	}
	attrs := gotBody["data"].(map[string]any)["attributes"].(map[string]any)
	if attrs["limit"] != "web:!web-2" {
		t.Fatalf("limit sent = %v", attrs["limit"])
	}
	out := resultText(t, res)
	// of-host-count is what turns "1 host" into "1 of 12", which is the blast
	// radius the agent is being asked to show.
	for _, want := range []string{`"web-1"`, `"host-count":1`, `"of-host-count":12`, `"taken-at"`} {
		if !strings.Contains(out, want) {
			t.Fatalf("result missing %s: %s", want, out)
		}
	}
}

func TestInventoryLimitPreviewToolReportsAPatternThatSelectsNothing(t *testing.T) {
	sess := inventoryToolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
		// `hosts` is omitted entirely rather than sent as `[]`: that is the
		// shape that reaches the SDK as a nil slice, so it is the one that
		// pins the normalisation. A server sending `[]` needs no code at all.
		_, _ = w.Write([]byte(`{"data":{"id":"invlp-2","type":"inventory-limit-previews",
		  "attributes":{"limit":"nope","host-count":0,"of-host-count":12,
		  "taken-at":"2026-10-07T10:00:00Z"}}}`))
	})
	res := callInventoryTool(t, sess, "terrapod_inventory_limit_preview",
		map[string]any{"inventory_id": "inv-1", "limit": "nope"})
	if res.IsError {
		t.Fatalf("selecting nothing is an answer, not an error: %s", resultText(t, res))
	}
	if out := resultText(t, res); !strings.Contains(out, `"hosts":[]`) {
		t.Fatalf("hosts should be an empty array, not null: %s", out)
	}
}

// An omitted limit expands to every host server-side. The tool refuses it so
// the broadest target set is always something the agent asked for.
func TestInventoryLimitPreviewToolRefusesAnEmptyPattern(t *testing.T) {
	called := false
	sess := inventoryToolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
		called = true
		_, _ = w.Write([]byte(`{"data":{"id":"x","type":"inventory-limit-previews","attributes":{}}}`))
	})
	for _, args := range []map[string]any{
		{"inventory_id": "inv-1"},
		{"inventory_id": "inv-1", "limit": ""},
		{"limit": "all"},
	} {
		res := callInventoryTool(t, sess, "terrapod_inventory_limit_preview", args)
		if !res.IsError {
			t.Fatalf("want a tool error for %v", args)
		}
	}
	if called {
		t.Fatal("an empty pattern must not reach Terrapod")
	}
	// `all` is the explicit way to ask for everything, and must work.
	sess2 := inventoryToolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"data":{"id":"invlp-3","type":"inventory-limit-previews",
		  "attributes":{"limit":"all","hosts":["web-1"],"host-count":1,"of-host-count":1,
		  "taken-at":"2026-10-07T10:00:00Z"}}}`))
	})
	if res := callInventoryTool(t, sess2, "terrapod_inventory_limit_preview",
		map[string]any{"inventory_id": "inv-1", "limit": "all"}); res.IsError {
		t.Fatalf("'all' must be accepted: %s", resultText(t, res))
	}
}

func TestInventoryRefreshToolPostsTheResolveAction(t *testing.T) {
	var gotPath, gotMethod string
	sess := inventoryToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		gotPath, gotMethod = r.URL.Path, r.Method
		_, _ = w.Write([]byte(`{"data":{"id":"invver-3","type":"inventory-versions","attributes":{
		  "host-count":4,"group-count":2,"produced-by":"api","taken-at":"2026-10-07T12:00:00Z",
		  "hosts":{"web-1":{},"web-2":{},"db-1":{},"db-2":{}},
		  "groups":{"web":["web-1","web-2"],"db":["db-1","db-2"]},
		  "ansible-inventory":{"_meta":{"hostvars":{}}}}}}`))
	})

	res := callInventoryTool(t, sess, "terrapod_inventory_refresh",
		map[string]any{"inventory_id": "inv-1"})
	if res.IsError {
		t.Fatalf("tool reported an error: %s", resultText(t, res))
	}
	if gotMethod != http.MethodPost || gotPath != "/api/v1/inventories/inv-1/actions/resolve" {
		t.Fatalf("%s %s", gotMethod, gotPath)
	}
	out := resultText(t, res)
	for _, want := range []string{`"host-count":4`, `"taken-at":"2026-10-07T12:00:00Z"`, `"db-1"`} {
		if !strings.Contains(out, want) {
			t.Fatalf("result missing %s: %s", want, out)
		}
	}
	if strings.Contains(out, "ansible-inventory") {
		t.Fatalf("a refresh returns the exhaustive maps, not the lossy rendering: %s", out)
	}
}

func TestInventoryToolsRequireTheirIDs(t *testing.T) {
	called := false
	sess := inventoryToolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
		called = true
		_, _ = w.Write([]byte(`{"data":[]}`))
	})
	for _, tc := range []struct {
		tool string
		args map[string]any
	}{
		{"terrapod_inventory_list", map[string]any{}},
		{"terrapod_workspace_inventories", map[string]any{}},
		{"terrapod_inventory_resolved", map[string]any{}},
		{"terrapod_inventory_versions", map[string]any{}},
		{"terrapod_inventory_refresh", map[string]any{}},
	} {
		res := callInventoryTool(t, sess, tc.tool, tc.args)
		if !res.IsError {
			t.Errorf("%s accepted a missing id", tc.tool)
		}
	}
	if called {
		t.Fatal("a missing id must not reach Terrapod")
	}
}
