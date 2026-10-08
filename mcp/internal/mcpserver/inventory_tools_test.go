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

// The ansible inventory tools (#1967, #1968) driven end to end: a real MCP
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

// invStructured decodes a tool's structured content into dst.
func invStructured(t *testing.T, res *mcp.CallToolResult, dst any) {
	t.Helper()
	if res.IsError {
		t.Fatalf("tool reported an error: %+v", res.Content)
	}
	raw, err := json.Marshal(res.StructuredContent)
	if err != nil {
		t.Fatalf("marshal structured content: %v", err)
	}
	if err := json.Unmarshal(raw, dst); err != nil {
		t.Fatalf("decode structured content %s: %v", raw, err)
	}
}

// invRoute answers a canned body per request path, and records what was asked.
// A route the test did not expect answers 404, so a tool addressing the wrong
// endpoint fails loudly rather than reading a body meant for another route.
type invRoute struct {
	paths   []string
	methods []string
	bodies  []string
}

func inventoryRouter(t *testing.T, routes map[string]string) (*mcp.ClientSession, *invRoute) {
	t.Helper()
	seen := &invRoute{}
	sess := inventoryToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		seen.paths = append(seen.paths, r.URL.Path)
		seen.methods = append(seen.methods, r.Method)
		raw, _ := io.ReadAll(r.Body)
		seen.bodies = append(seen.bodies, string(raw))
		body, ok := routes[r.Method+" "+r.URL.Path]
		if !ok {
			if body, ok = routes[r.URL.Path]; !ok {
				w.WriteHeader(http.StatusNotFound)
				_, _ = w.Write([]byte(`{"errors":[{"detail":"no fixture for this route","status":"404"}]}`))
				return
			}
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		if body == "" {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		_, _ = w.Write([]byte(body))
	})
	return sess, seen
}

const invHostBody = `{"data":{"id":"invhost-1","type":"inventory-hosts","attributes":{
  "name":"web-1","group-count":2,"variable-count":1,
  "created-at":"2026-10-07T09:00:00Z","updated-at":"2026-10-07T09:00:00Z"},
  "relationships":{"workspace":{"data":{"id":"ws-1","type":"workspaces"}}}}}`

const invGroupBody = `{"data":{"id":"invgroup-1","type":"inventory-groups","attributes":{
  "name":"web","member-count":1,"child-count":1,"variable-count":1,
  "created-at":"2026-10-07T09:00:00Z","updated-at":"2026-10-07T09:00:00Z"},
  "relationships":{"workspace":{"data":{"id":"ws-1","type":"workspaces"}}}}}`

const invHostListBody = `{"data":[{"id":"invhost-1","type":"inventory-hosts","attributes":{
  "name":"web-1","group-count":2,"variable-count":1}}],
  "meta":{"pagination":{"current-page":1,"page-size":100,"total-pages":1,"total-count":1}}}`

const invGroupListBody = `{"data":[{"id":"invgroup-1","type":"inventory-groups","attributes":{
  "name":"web","member-count":1,"child-count":1,"variable-count":1}}],
  "meta":{"pagination":{"current-page":1,"page-size":100,"total-pages":1,"total-count":1}}}`

// ── Reads ────────────────────────────────────────────────────────────────────

// A workspace with no settings row is the NORMAL default, so the tool must
// report it rather than relaying a 404 as a problem.
func TestInventorySettingsReportsAnUnconfiguredWorkspaceWithoutAnError(t *testing.T) {
	sess := inventoryToolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"errors":[{"detail":"Not found","status":"404"}]}`))
	})

	res := callInventoryTool(t, sess, "terrapod_inventory_settings",
		map[string]any{"workspace_id": "ws-1"})

	var out struct {
		Configured bool `json:"configured"`
	}
	invStructured(t, res, &out)
	if out.Configured {
		t.Error("an absent settings row must read as unconfigured")
	}
}

func TestInventorySettingsReadsTheBinding(t *testing.T) {
	sess, seen := inventoryRouter(t, map[string]string{
		"/api/v1/workspaces/ws-1/inventory/settings": `{"data":{"id":"ws-1",
		  "type":"inventory-settings","attributes":{"include-platform":true,
		  "repo-url":"https://example.invalid/org/ansible","branch":"main",
		  "working-directory":"inventory","ignore-paths":["archive/"]},
		  "relationships":{"workspace":{"data":{"id":"ws-1","type":"workspaces"}},
		    "vcs-connection":{"data":{"id":"vcs-7","type":"vcs-connections"}}}}}`,
	})

	res := callInventoryTool(t, sess, "terrapod_inventory_settings",
		map[string]any{"workspace_id": "ws-1"})

	var out struct {
		Configured bool `json:"configured"`
		Settings   struct {
			VCSConnectionID  string `json:"vcs-connection-id"`
			WorkingDirectory string `json:"working-directory"`
		} `json:"settings"`
	}
	invStructured(t, res, &out)
	if !out.Configured || out.Settings.VCSConnectionID != "vcs-7" {
		t.Errorf("binding not surfaced: %+v", out)
	}
	if out.Settings.WorkingDirectory != "inventory" {
		t.Errorf("working directory: %q", out.Settings.WorkingDirectory)
	}
	if len(seen.paths) != 1 {
		t.Errorf("one call expected, got %v", seen.paths)
	}
}

func TestInventoryListReadsHostsGroupsAndTheVarsUnderAll(t *testing.T) {
	sess, seen := inventoryRouter(t, map[string]string{
		"/api/v1/workspaces/ws-1/inventory/hosts":  invHostListBody,
		"/api/v1/workspaces/ws-1/inventory/groups": invGroupListBody,
		"/api/v1/workspaces/ws-1/inventory/vars": `{"data":[{"id":"invvar-1",
		  "type":"inventory-global-vars","attributes":{"key":"ntp","value":"pool",
		  "structured":false,"sensitive":false}}]}`,
	})

	res := callInventoryTool(t, sess, "terrapod_inventory_list",
		map[string]any{"workspace_id": "ws-1"})

	var out struct {
		HostCount  int `json:"host_count"`
		GroupCount int `json:"group_count"`
		Hosts      []struct {
			Name       string `json:"name"`
			GroupCount int    `json:"group-count"`
		} `json:"hosts"`
		VarsUnderAll []struct {
			Key string `json:"key"`
		} `json:"vars_under_all"`
	}
	invStructured(t, res, &out)

	if out.HostCount != 1 || out.GroupCount != 1 {
		t.Errorf("counts: %+v", out)
	}
	// Counts, not the rows -- a listing must not grow with the inventory.
	if out.Hosts[0].GroupCount != 2 {
		t.Errorf("per-host link count not surfaced: %+v", out.Hosts[0])
	}
	if len(out.VarsUnderAll) != 1 || out.VarsUnderAll[0].Key != "ntp" {
		t.Errorf("group_vars/all not surfaced: %+v", out.VarsUnderAll)
	}
	// All three collections, and the vars one addresses the WORKSPACE because
	// `all` cannot be a group.
	want := "/api/v1/workspaces/ws-1/inventory/vars"
	if !containsPath(seen.paths, want) {
		t.Errorf("want %s among %v", want, seen.paths)
	}
}

// An empty inventory is the common answer, not an edge case, and a nil slice
// marshals to `null`, which fails the derived output schema and costs the agent
// the whole result.
func TestInventoryListReturnsEmptyListsRatherThanNull(t *testing.T) {
	sess, _ := inventoryRouter(t, map[string]string{
		"/api/v1/workspaces/ws-1/inventory/hosts":  `{"data":[]}`,
		"/api/v1/workspaces/ws-1/inventory/groups": `{"data":[]}`,
		"/api/v1/workspaces/ws-1/inventory/vars":   `{"data":[]}`,
	})

	res := callInventoryTool(t, sess, "terrapod_inventory_list",
		map[string]any{"workspace_id": "ws-1"})
	if res.IsError {
		t.Fatalf("an empty inventory must not be an error: %+v", res.Content)
	}
	raw, _ := json.Marshal(res.StructuredContent)
	for _, field := range []string{`"hosts":[]`, `"groups":[]`, `"vars_under_all":[]`} {
		if !strings.Contains(string(raw), field) {
			t.Errorf("want %s in %s", field, raw)
		}
	}
}

// The id prefix is the discriminator, so there is no kind argument to mistype
// -- and a host and a group must reach different endpoints from one tool.
func TestInventoryDetailDispatchesOnTheIDPrefix(t *testing.T) {
	t.Run("host", func(t *testing.T) {
		sess, seen := inventoryRouter(t, map[string]string{
			"/api/v1/inventory-hosts/invhost-1": invHostBody,
			"/api/v1/inventory-hosts/invhost-1/vars": `{"data":[{"id":"invhvar-1",
			  "type":"inventory-host-vars","attributes":{"key":"ansible_host",
			  "value":"10.0.0.4","structured":false,"sensitive":false}}]}`,
			"/api/v1/inventory-hosts/invhost-1/groups": `{"data":[{"id":"invhg-1",
			  "type":"inventory-host-groups","attributes":{},
			  "relationships":{"host":{"data":{"id":"invhost-1","type":"inventory-hosts"}},
			    "group":{"data":{"id":"invgroup-1","type":"inventory-groups"}}}}]}`,
		})

		res := callInventoryTool(t, sess, "terrapod_inventory_detail",
			map[string]any{"id": "invhost-1"})

		var out struct {
			Host *struct {
				Name string `json:"name"`
			} `json:"host"`
			Group *json.RawMessage `json:"group"`
			Vars  []struct {
				Key string `json:"key"`
			} `json:"vars"`
			Groups []struct {
				ID string `json:"id"`
			} `json:"groups"`
		}
		invStructured(t, res, &out)
		if out.Host == nil || out.Host.Name != "web-1" {
			t.Fatalf("host not surfaced: %+v", out)
		}
		if out.Group != nil {
			t.Error("a host detail must not carry a group")
		}
		if len(out.Vars) != 1 || len(out.Groups) != 1 {
			t.Errorf("a host's variables and memberships must both come back: %+v", out)
		}
		for _, p := range seen.paths {
			if strings.Contains(p, "inventory-groups/") {
				t.Errorf("a host id must not reach a group endpoint: %s", p)
			}
		}
	})

	t.Run("group", func(t *testing.T) {
		sess, _ := inventoryRouter(t, map[string]string{
			"/api/v1/inventory-groups/invgroup-1":          invGroupBody,
			"/api/v1/inventory-groups/invgroup-1/vars":     `{"data":[]}`,
			"/api/v1/inventory-groups/invgroup-1/hosts":    `{"data":[]}`,
			"/api/v1/inventory-groups/invgroup-1/children": `{"data":[{"id":"invgc-1","type":"inventory-group-children","attributes":{}}]}`,
			"/api/v1/inventory-groups/invgroup-1/parents":  `{"data":[]}`,
		})

		res := callInventoryTool(t, sess, "terrapod_inventory_detail",
			map[string]any{"id": "invgroup-1"})

		var out struct {
			Group *struct {
				Name string `json:"name"`
			} `json:"group"`
			Children []struct {
				ID string `json:"id"`
			} `json:"children"`
		}
		invStructured(t, res, &out)
		if out.Group == nil || out.Group.Name != "web" {
			t.Fatalf("group not surfaced: %+v", out)
		}
		// A group may have several parents, so both directions are read.
		if len(out.Children) != 1 {
			t.Errorf("nesting not surfaced: %+v", out)
		}
	})

	t.Run("an id of neither kind is refused before any call", func(t *testing.T) {
		var called int
		sess := inventoryToolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
			called++
			w.WriteHeader(http.StatusOK)
		})
		res := callInventoryTool(t, sess, "terrapod_inventory_detail",
			map[string]any{"id": "invhvar-9"})
		if !res.IsError {
			t.Error("a variable id is not a host or a group")
		}
		if called != 0 {
			t.Errorf("nothing should have been called, got %d requests", called)
		}
	})
}

func TestInventoryResolvedCarriesTheNestingAlongsideDirectMembership(t *testing.T) {
	sess, seen := inventoryRouter(t, map[string]string{
		"/api/v1/workspaces/ws-1/inventory/resolved": `{"data":{"id":"ws-1",
		  "type":"resolved-inventories","attributes":{
		  "hosts":{"web-1":{"ansible_host":"10.0.0.4"},"switch1":{}},
		  "groups":{"web":["web-1"],"net":["switch1"],"edge":[]},
		  "group-children":{"edge":["net"]},
		  "host-count":2,"group-count":3},
		  "relationships":{"workspace":{"data":{"id":"ws-1","type":"workspaces"}}}}}`,
	})

	res := callInventoryTool(t, sess, "terrapod_inventory_resolved",
		map[string]any{"workspace_id": "ws-1"})

	var out struct {
		Hosts         map[string]map[string]any `json:"hosts"`
		Groups        map[string][]string       `json:"groups"`
		GroupChildren map[string][]string       `json:"group-children"`
	}
	invStructured(t, res, &out)

	// A var-less host is omitted from ansible's own `_meta.hostvars`, so losing
	// it here would silently shrink every target set derived from this field.
	if _, ok := out.Hosts["switch1"]; !ok {
		t.Error("a host with no variables was dropped")
	}
	// A parent whose members all arrive through a child reports an EMPTY direct
	// list, and the nesting is the only thing that says it reaches anything.
	if len(out.Groups["edge"]) != 0 {
		t.Errorf("edge should have no direct members: %v", out.Groups["edge"])
	}
	if len(out.GroupChildren["edge"]) != 1 {
		t.Errorf("the nesting must come back, or an agent reads edge as empty: %v",
			out.GroupChildren)
	}
	if seen.paths[0] != "/api/v1/workspaces/ws-1/inventory/resolved" {
		t.Errorf("wrong route: %s", seen.paths[0])
	}
}

// `limit` goes to the same endpoint as a query, and `~regex` reaches ansible
// untouched -- an earlier implementation refused it, which is what this
// inverts.
func TestInventoryResolvedPassesTheLimitThroughIncludingARegex(t *testing.T) {
	var query string
	sess := inventoryToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		query = r.URL.Query().Get("limit")
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(`{"data":{"id":"ws-1","type":"resolved-inventories",
		  "attributes":{"hosts":{"web-1":{}},"groups":{},"group-children":{},
		  "host-count":1,"group-count":0,"limit":"~^web"}}}`))
	})

	res := callInventoryTool(t, sess, "terrapod_inventory_resolved",
		map[string]any{"workspace_id": "ws-1", "limit": "~^web"})
	if res.IsError {
		t.Fatalf("a regex limit must not be refused: %+v", res.Content)
	}
	if query != "~^web" {
		t.Errorf("the limit must arrive intact, got %q", query)
	}
}

// Resolution fails closed: an error must surface rather than yielding an empty
// host set, because a configure targeting too little is worse than no answer.
func TestInventoryResolvedFailsClosed(t *testing.T) {
	sess := inventoryToolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte(`{"errors":[{"detail":"ansible could not be obtained","status":"503"}]}`))
	})

	res := callInventoryTool(t, sess, "terrapod_inventory_resolved",
		map[string]any{"workspace_id": "ws-1"})
	if !res.IsError {
		t.Error("a failed resolution must be an error, not an empty host set")
	}
}

// ── Writes ───────────────────────────────────────────────────────────────────

func TestInventoryHostAndGroupDeclare(t *testing.T) {
	t.Run("host", func(t *testing.T) {
		sess, seen := inventoryRouter(t, map[string]string{
			"POST /api/v1/workspaces/ws-1/inventory/hosts": invHostBody,
		})
		res := callInventoryTool(t, sess, "terrapod_inventory_host_declare",
			map[string]any{"workspace_id": "ws-1", "name": "web-1"})
		if res.IsError {
			t.Fatalf("declare failed: %+v", res.Content)
		}
		if !strings.Contains(seen.bodies[0], `"name":"web-1"`) {
			t.Errorf("name not sent: %s", seen.bodies[0])
		}
	})
	t.Run("group", func(t *testing.T) {
		sess, seen := inventoryRouter(t, map[string]string{
			"POST /api/v1/workspaces/ws-1/inventory/groups": invGroupBody,
		})
		res := callInventoryTool(t, sess, "terrapod_inventory_group_declare",
			map[string]any{"workspace_id": "ws-1", "name": "web"})
		if res.IsError {
			t.Fatalf("declare failed: %+v", res.Content)
		}
		if seen.paths[0] != "/api/v1/workspaces/ws-1/inventory/groups" {
			t.Errorf("wrong route: %s", seen.paths[0])
		}
	})
}

// One tool makes one link, and which one it makes is decided by which id is
// passed -- so passing both is refused rather than silently picking.
func TestInventoryLinkMakesOneLinkOfTheRightKind(t *testing.T) {
	t.Run("membership", func(t *testing.T) {
		sess, seen := inventoryRouter(t, map[string]string{
			"POST /api/v1/inventory-groups/invgroup-1/hosts": `{"data":{"id":"invhg-1",
			  "type":"inventory-host-groups","attributes":{},
			  "relationships":{"host":{"data":{"id":"invhost-1","type":"inventory-hosts"}},
			    "group":{"data":{"id":"invgroup-1","type":"inventory-groups"}}}}}`,
		})
		res := callInventoryTool(t, sess, "terrapod_inventory_link",
			map[string]any{"group_id": "invgroup-1", "host_id": "invhost-1"})
		var out struct {
			Membership *struct {
				ID string `json:"id"`
			} `json:"membership"`
			Nesting *json.RawMessage `json:"nesting"`
		}
		invStructured(t, res, &out)
		if out.Membership == nil || out.Membership.ID != "invhg-1" {
			t.Fatalf("membership not surfaced: %+v", out)
		}
		if out.Nesting != nil {
			t.Error("a membership must not come back as a nesting")
		}
		// The host travels as a RELATIONSHIP; the group is in the path.
		if !strings.Contains(seen.bodies[0], `"invhost-1"`) {
			t.Errorf("host relationship not sent: %s", seen.bodies[0])
		}
	})

	t.Run("nesting", func(t *testing.T) {
		sess, seen := inventoryRouter(t, map[string]string{
			"POST /api/v1/inventory-groups/invgroup-1/children": `{"data":{"id":"invgc-1",
			  "type":"inventory-group-children","attributes":{}}}`,
		})
		res := callInventoryTool(t, sess, "terrapod_inventory_link",
			map[string]any{"group_id": "invgroup-1", "child_group_id": "invgroup-2"})
		var out struct {
			Nesting *struct {
				ID string `json:"id"`
			} `json:"nesting"`
		}
		invStructured(t, res, &out)
		if out.Nesting == nil || out.Nesting.ID != "invgc-1" {
			t.Fatalf("nesting not surfaced: %+v", out)
		}
		if seen.paths[0] != "/api/v1/inventory-groups/invgroup-1/children" {
			t.Errorf("wrong route: %s", seen.paths[0])
		}
		_ = seen.bodies
	})

	t.Run("both is refused before any call", func(t *testing.T) {
		var called int
		sess := inventoryToolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
			called++
			w.WriteHeader(http.StatusOK)
		})
		res := callInventoryTool(t, sess, "terrapod_inventory_link", map[string]any{
			"group_id": "invgroup-1", "host_id": "invhost-1", "child_group_id": "invgroup-2",
		})
		if !res.IsError {
			t.Error("one call makes one link")
		}
		if called != 0 {
			t.Errorf("nothing should have been called, got %d", called)
		}
	})
}

// "Set" has to mean set: an agent asked to change a variable should not have to
// know whether it is already there, so a conflict is recovered by patching.
func TestInventoryVarSetCreatesThenPatchesOnConflict(t *testing.T) {
	var methods []string
	sess := inventoryToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		methods = append(methods, r.Method+" "+r.URL.Path)
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch r.Method {
		case http.MethodPost:
			w.WriteHeader(http.StatusConflict)
			_, _ = w.Write([]byte(`{"errors":[{"detail":"already exists","status":"409"}]}`))
		case http.MethodGet:
			_, _ = w.Write([]byte(`{"data":[{"id":"invhvar-1","type":"inventory-host-vars",
			  "attributes":{"key":"ansible_host","value":"10.0.0.4"}}]}`))
		default:
			_, _ = w.Write([]byte(`{"data":{"id":"invhvar-1","type":"inventory-host-vars",
			  "attributes":{"key":"ansible_host","value":"10.0.0.9"}}}`))
		}
	})

	res := callInventoryTool(t, sess, "terrapod_inventory_var_set", map[string]any{
		"host_id": "invhost-1", "key": "ansible_host", "value": "10.0.0.9",
	})
	if res.IsError {
		t.Fatalf("a conflict must be recovered, not relayed: %+v", res.Content)
	}
	want := []string{
		"POST /api/v1/inventory-hosts/invhost-1/vars",
		"GET /api/v1/inventory-hosts/invhost-1/vars",
		"PATCH /api/v1/inventory-host-vars/invhvar-1",
	}
	if len(methods) != 3 {
		t.Fatalf("want create, locate, patch; got %v", methods)
	}
	for i := range want {
		if methods[i] != want[i] {
			t.Errorf("step %d: got %s, want %s", i, methods[i], want[i])
		}
	}
}

func TestInventoryVarSetNeedsExactlyOneScope(t *testing.T) {
	cases := []map[string]any{
		{"key": "k", "value": "v"},
		{"key": "k", "value": "v", "host_id": "invhost-1", "group_id": "invgroup-1"},
		{"key": "k", "value": "v", "host_id": "invhost-1", "workspace_id": "ws-1"},
	}
	for _, args := range cases {
		var called int
		sess := inventoryToolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
			called++
			w.WriteHeader(http.StatusOK)
		})
		res := callInventoryTool(t, sess, "terrapod_inventory_var_set", args)
		if !res.IsError {
			t.Errorf("%v should be refused: a variable lives in exactly one place", args)
		}
		if called != 0 {
			t.Errorf("%v reached the API before being refused", args)
		}
	}
}

func TestInventoryVarSetAddressesTheRightScope(t *testing.T) {
	cases := []struct {
		name string
		args map[string]any
		path string
	}{
		{"host", map[string]any{"host_id": "invhost-1", "key": "k", "value": "v"},
			"/api/v1/inventory-hosts/invhost-1/vars"},
		{"group", map[string]any{"group_id": "invgroup-1", "key": "k", "value": "v"},
			"/api/v1/inventory-groups/invgroup-1/vars"},
		// `all` is parented on the WORKSPACE, because it cannot be a group.
		{"all", map[string]any{"workspace_id": "ws-1", "key": "k", "value": "v"},
			"/api/v1/workspaces/ws-1/inventory/vars"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			sess, seen := inventoryRouter(t, map[string]string{
				"POST " + tc.path: `{"data":{"id":"invvar-1","type":"inventory-global-vars",
				  "attributes":{"key":"k","value":"v"}}}`,
			})
			res := callInventoryTool(t, sess, "terrapod_inventory_var_set", tc.args)
			if res.IsError {
				t.Fatalf("set failed: %+v", res.Content)
			}
			if seen.paths[0] != tc.path {
				t.Errorf("got %s, want %s", seen.paths[0], tc.path)
			}
		})
	}
}

// The typed prefix is the discriminator, so there is no way to delete a kind
// other than the one the id names.
func TestInventoryRemoveDispatchesEveryPrefix(t *testing.T) {
	cases := []struct{ id, path, label string }{
		{"invhost-1", "/api/v1/inventory-hosts/invhost-1", "host"},
		{"invgroup-1", "/api/v1/inventory-groups/invgroup-1", "group"},
		{"invhvar-1", "/api/v1/inventory-host-vars/invhvar-1", "host variable"},
		{"invgvar-1", "/api/v1/inventory-group-vars/invgvar-1", "group variable"},
		{"invvar-1", "/api/v1/inventory-global-vars/invvar-1", "variable under all"},
		{"invhg-1", "/api/v1/inventory-host-groups/invhg-1", "group membership"},
		{"invgc-1", "/api/v1/inventory-group-children/invgc-1", "group nesting"},
		{"ws-1", "/api/v1/workspaces/ws-1/inventory/settings", "inventory settings"},
	}
	for _, tc := range cases {
		t.Run(tc.id, func(t *testing.T) {
			sess, seen := inventoryRouter(t, map[string]string{
				"DELETE " + tc.path: "",
			})
			res := callInventoryTool(t, sess, "terrapod_inventory_remove",
				map[string]any{"id": tc.id})
			var out struct {
				Removed string `json:"removed"`
			}
			invStructured(t, res, &out)
			if seen.paths[0] != tc.path || seen.methods[0] != http.MethodDelete {
				t.Errorf("got %s %s, want DELETE %s", seen.methods[0], seen.paths[0], tc.path)
			}
			// The label is what an agent relays to a user, so it has to name
			// what actually went.
			if out.Removed != tc.label {
				t.Errorf("label: got %q, want %q", out.Removed, tc.label)
			}
		})
	}
}

// `invgvar-` and `invgroup-` and `invgc-` all begin `invg`, so the prefixes are
// matched in order rather than by a shortest match. An id of no inventory kind
// must be refused before anything is called.
func TestInventoryRemoveRefusesAnIDOfNoInventoryKind(t *testing.T) {
	for _, id := range []string{"", "run-1", "invg", "var-1"} {
		var called int
		sess := inventoryToolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
			called++
			w.WriteHeader(http.StatusNoContent)
		})
		res := callInventoryTool(t, sess, "terrapod_inventory_remove",
			map[string]any{"id": id})
		if !res.IsError {
			t.Errorf("%q is not an inventory row", id)
		}
		if called != 0 {
			t.Errorf("%q reached the API before being refused", id)
		}
	}
}

// ── The description guard ────────────────────────────────────────────────────

// TestTheInventoryToolsDoNotTellAnAgentAReadIsStale guards the claim an agent
// acts on, which no other gate reaches.
//
// This exists because the descriptions it checks were FALSE for a release and
// every gate was green. They said a read served "the newest snapshot", that
// the limit preview was "only as FRESH as the last snapshot, not live", and
// that an agent finding `taken-at` old should refresh before previewing. Each
// was true when written and stopped being true when the code moved underneath
// it — and nothing noticed, because the catalogue golden freezes names,
// annotations and input schemas and NOT descriptions (see liveToolsByName).
// The docs had a gate quoting the API's own literals and the web tab had an
// e2e assertion; MCP had neither, so MCP is the surface that shipped it.
//
// It is deliberately NOT a golden of description text. A golden catches an
// unreviewed EDIT, and nobody edited these. What is pinned is the property.
//
// # The property has changed twice, and so has this guard
//
// It first required the liveness claim to carry a CONDITION — "for an
// inventory Terrapod can resolve itself" — because an inventory holding a
// source that needed ansible really was served from a snapshot. Dynamic
// inventory was then declined outright (#1970), so every source is static,
// every read resolves, and there is no longer a case the condition would
// exclude. A condition that can never fail is not caution; it tells an agent
// to doubt an answer that is always current.
//
// The second change is this group gaining WRITE tools. The descriptions used
// to say a declared host was owned by Terraform and could only be edited
// through a configuration. That argument was wrong rather than merely stale:
// the collision it feared is a 409 the practitioner resolves with an import,
// which is how every other Terraform-managed thing here behaves, and
// declining the write never prevented the collision. So "reads only" and its
// phrasings join the retired list too.
func TestTheInventoryToolsDoNotTellAnAgentAReadIsStale(t *testing.T) {
	tools := liveToolsByName(t)

	// Phrasings that were true before the read path became live, and are now
	// the wrong answer in the direction that matters: an agent told its data
	// may be stale either refuses to act on it or reaches for a refresh tool
	// that no longer exists.
	retired := []string{
		"only as FRESH as the last snapshot",
		"only as fresh as the last snapshot",
		"Serves the newest snapshot",
		"stale target set is brought up to date",
		// The condition itself, now that nothing can fail it.
		"resolve itself",
		"needs ansible",
		"taken-at",
		// The read-only argument, now that the writes exist.
		"read-only",
		"reads only",
		"no tool here declares",
		// The retired model's own vocabulary. A description naming any of
		// these is describing a shape that no longer exists.
		"inventory_item",
		"ordered sources",
		"api-resolvable",
	}
	for name, desc := range tools {
		if !strings.HasPrefix(name, "terrapod_inventory") {
			continue
		}
		for _, phrase := range retired {
			if strings.Contains(desc, phrase) {
				t.Errorf("%s carries a retired claim (%q): every source is static, so the "+
					"read resolves the rows to answer the call, and the write tools exist",
					name, phrase)
			}
		}
	}

	// And the tool whose answer an operator acts on must SAY the read is live.
	// Asserting the absence of the retired phrasing alone would pass on a
	// description that said nothing at all, which is the same disservice more
	// quietly.
	desc, ok := tools["terrapod_inventory_resolved"]
	if !ok {
		t.Fatal("terrapod_inventory_resolved is not registered")
	}
	if !strings.Contains(desc, "LIVE") && !strings.Contains(desc, "live") {
		t.Error("terrapod_inventory_resolved does not tell an agent the resolution is live")
	}
	// The misreading this field exists to prevent has to be named where an
	// agent will meet it, or an empty direct-membership list reads as "targets
	// nothing" for a parent that reaches every host through a child.
	if !strings.Contains(desc, "DIRECT membership") {
		t.Error("terrapod_inventory_resolved does not warn that a group's host list is " +
			"direct membership only, so a nesting-only parent reads as empty")
	}

	// There must be no refresh tool and no history. One existed, and its
	// description had to talk an agent out of using it; removing the snapshot
	// removed the reason for both. If one comes back, its description is a
	// claim this guard has no opinion on yet — so fail here and make that a
	// deliberate decision.
	for name := range tools {
		if strings.Contains(name, "inventory_refresh") ||
			strings.Contains(name, "inventory_versions") {
			t.Errorf("%s is registered; a read is live and writes nothing, so a refresh "+
				"or a history is a surface that needs its own justification", name)
		}
	}
}

// Every one of the eight structures has to be reachable, because the rule is
// that an agent can observe or drive what the platform can. A structure with no
// tool is a gap nothing else would report: the catalogue golden pins the tools
// that exist, not the ones that should.
func TestEveryInventoryStructureIsReachable(t *testing.T) {
	tools := liveToolsByName(t)
	for _, name := range []string{
		"terrapod_inventory_settings",
		"terrapod_inventory_settings_set",
		"terrapod_inventory_list",
		"terrapod_inventory_detail",
		"terrapod_inventory_resolved",
		"terrapod_inventory_host_declare",
		"terrapod_inventory_group_declare",
		"terrapod_inventory_link",
		"terrapod_inventory_var_set",
		"terrapod_inventory_remove",
	} {
		if _, ok := tools[name]; !ok {
			t.Errorf("%s is not registered", name)
		}
	}
}

func containsPath(paths []string, want string) bool {
	for _, p := range paths {
		if p == want {
			return true
		}
	}
	return false
}
