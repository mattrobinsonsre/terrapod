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

// The inventory harness registers only the inventory tools, so the crud tools
// need their own caller. Same shape, one registration call different.
func crudRouter(t *testing.T, routes map[string]string) (*mcp.ClientSession, *invRoute) {
	t.Helper()
	seen := &invRoute{}
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
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
	}))
	t.Cleanup(api.Close)

	c, err := terrapod.NewClient(terrapod.Options{BaseURL: api.URL, Token: "t"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	srv := mcp.NewServer(&mcp.Implementation{Name: "test", Version: "0"}, nil)
	registerCRUD(srv, c)

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
	return sess, seen
}

// `terrapod_variable_delete` had NO test, and it compared categories with `==`
// where the SDK exports `SameCategory` for exactly this (#2715). The list it
// filters comes from `ListVariables`, which reads the native surface, so a
// stored category is `native` -- while this tool's own field description tells
// an agent the primary spelling is `terraform` and that "native is accepted for
// terraform". So following the documentation matched nothing and the tool
// answered "not found" for a variable that is plainly there.
//
// These drive the real tool through a real client against a fake Terrapod, and
// pin the fold in both directions: the documented spelling must reach a
// native-surface row, and a genuinely different category must still not.

const varsTwoCategories = `{"data":[
  {"id":"var-1","type":"vars","attributes":{"key":"AWS_REGION","category":"native","value":"eu-west-1","sensitive":false}},
  {"id":"var-2","type":"vars","attributes":{"key":"AWS_REGION","category":"env","value":"eu-west-1","sensitive":false}}],
  "meta":{"pagination":{"current-page":1,"page-size":100,"total-count":2,"total-pages":1}}}`

const varsOneNative = `{"data":[
  {"id":"var-1","type":"vars","attributes":{"key":"AWS_REGION","category":"native","value":"eu-west-1","sensitive":false}}],
  "meta":{"pagination":{"current-page":1,"page-size":100,"total-count":1,"total-pages":1}}}`

func TestVariableDeleteFoldsTheDocumentedCategorySpelling(t *testing.T) {
	// `terraform` is what the field description names first; the row says
	// `native`. Under `==` this was a 404 with no way for the agent to proceed.
	sess, seen := crudRouter(t, map[string]string{
		"GET /api/v1/workspaces/ws-abc/vars":          varsTwoCategories,
		"DELETE /api/v1/workspaces/ws-abc/vars/var-1": "",
	})

	res := callInventoryTool(t, sess, "terrapod_variable_delete",
		map[string]any{"workspace_id": "ws-abc", "key": "AWS_REGION", "category": "terraform"})

	if res.IsError {
		t.Fatalf("`terraform` must reach the native-surface row, got an error: %+v", res.Content)
	}
	var hit bool
	for i, p := range seen.paths {
		if p == "/api/v1/workspaces/ws-abc/vars/var-1" && seen.methods[i] == "DELETE" {
			hit = true
		}
	}
	if !hit {
		t.Fatalf("expected a DELETE of var-1 (the native row); saw %v %v", seen.methods, seen.paths)
	}
}

func TestVariableDeleteStillRefusesAGenuinelyDifferentCategory(t *testing.T) {
	// The fold must not swallow the distinction it exists to bridge: `env` is a
	// different category, not another spelling of the native one.
	sess, seen := crudRouter(t, map[string]string{
		"GET /api/v1/workspaces/ws-abc/vars": varsOneNative,
	})

	res := callInventoryTool(t, sess, "terrapod_variable_delete",
		map[string]any{"workspace_id": "ws-abc", "key": "AWS_REGION", "category": "env"})

	if !res.IsError {
		t.Fatalf("`env` must not match a native row, but the delete was accepted")
	}
	for _, m := range seen.methods {
		if m == "DELETE" {
			t.Fatalf("nothing may be deleted when no category matches; saw %v", seen.methods)
		}
	}
}

func TestVariableDeleteStillRefusesAnAmbiguousKey(t *testing.T) {
	// With no category and the key in two, the refusal must still list them --
	// widening the comparison must not collapse two rows into one match.
	sess, seen := crudRouter(t, map[string]string{
		"GET /api/v1/workspaces/ws-abc/vars": varsTwoCategories,
	})

	res := callInventoryTool(t, sess, "terrapod_variable_delete",
		map[string]any{"workspace_id": "ws-abc", "key": "AWS_REGION"})

	if !res.IsError {
		t.Fatalf("an ambiguous key must be refused rather than guessed")
	}
	blob, _ := json.Marshal(res.Content)
	for _, want := range []string{"native", "env"} {
		if !strings.Contains(string(blob), want) {
			t.Errorf("the refusal must name the categories so the agent can choose; %q missing from %s", want, blob)
		}
	}
	for _, m := range seen.methods {
		if m == "DELETE" {
			t.Fatalf("an ambiguous key must delete nothing; saw %v", seen.methods)
		}
	}
}
