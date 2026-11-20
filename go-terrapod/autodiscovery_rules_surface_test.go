package terrapod

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// newAutodiscSurfaceFixture serves the four routes the SDK gained for
// #2621: the collection, both previews and the scan.
//
// A fixture of its own rather than an extension of newAutodiscFixture,
// whose GET case is `HasPrefix("/api/v1/autodiscovery-rules/")` and so
// answers a PREVIEW request with a rule document -- which would make
// every assertion here pass against the wrong payload.
func newAutodiscSurfaceFixture(t *testing.T, seen *[]string) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Body != nil {
			_, _ = io.ReadAll(r.Body)
			_ = r.Body.Close()
		}
		if seen != nil {
			*seen = append(*seen, r.Method+" "+r.URL.RequestURI())
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/autodiscovery-rules":
			// Page 2 comes back short, which is how the paging helper
			// knows to stop.
			if strings.Contains(r.URL.RawQuery, "page%5Bnumber%5D=2") {
				_, _ = w.Write([]byte(`{"data":[{"id":"ad-bbb","type":"autodiscovery-rules","attributes":{"name":"second"}}]}`))
				return
			}
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"ad-aaa","type":"autodiscovery-rules","attributes":{"name":"monorepo","pattern":"services/*","enabled":true,"created-at":"2025-01-01T00:00:00Z"}},
			  {"id":"ad-bbb","type":"autodiscovery-rules","attributes":{"name":"second","enabled":false}}
			]}`))

		case r.Method == http.MethodPost && r.URL.Path == "/api/v1/autodiscovery-rules/preview":
			_, _ = w.Write([]byte(`{"data":{"type":"autodiscovery-rule-previews","attributes":{
			  "ref":"main","files-walked":12,
			  "entries":[{"workspace_name":"svc-a","working_directory":"services/a","collision":false,"existing_autodiscovered":false}]
			}}}`))

		case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/preview"):
			_, _ = w.Write([]byte(`{"data":{"type":"autodiscovery-rule-previews","attributes":{
			  "ref":"release/v1","files-walked":31,
			  "entries":[
			    {"workspace_name":"svc-a","working_directory":"services/a","collision":false,"existing_autodiscovered":true},
			    {"workspace_name":"svc-b","working_directory":"services/b","collision":true,"existing_autodiscovered":false}
			  ]
			}}}`))

		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/scan"):
			_, _ = w.Write([]byte(`{"data":{"type":"autodiscovery-rule-scans","attributes":{
			  "ref":"main","files-walked":31,"workspaces-created":2,
			  "workspace-ids":["ws-111","ws-222"]
			}}}`))

		default:
			http.Error(w, "unhandled", http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestListAutodiscoveryRules(t *testing.T) {
	c := newAutodiscSurfaceFixture(t, nil)
	rules, err := c.ListAutodiscoveryRules(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(rules) != 2 {
		t.Fatalf("want 2 rules, got %d", len(rules))
	}
	if rules[0].ID != "ad-aaa" || rules[1].ID != "ad-bbb" {
		t.Errorf("ids: %q, %q", rules[0].ID, rules[1].ID)
	}
	if rules[0].CreatedAt != "2025-01-01T00:00:00Z" {
		t.Errorf("created-at not parsed: %q", rules[0].CreatedAt)
	}
	// The attribute bag is the whole point of the untyped shape: a new
	// server attribute must arrive without an SDK change.
	if rules[0].Attributes["pattern"] != "services/*" {
		t.Errorf("attributes not carried: %v", rules[0].Attributes)
	}
	if rules[1].Attributes["enabled"] != false {
		t.Errorf("a false boolean must survive, not vanish: %v", rules[1].Attributes)
	}
}

func TestListAllAutodiscoveryRulesPages(t *testing.T) {
	var seen []string
	c := newAutodiscSurfaceFixture(t, &seen)
	rules, err := c.ListAllAutodiscoveryRules(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	// Page 1 is full at the fixture's two rows only if pageSize were 2;
	// it is 100, so the first page is already short and paging stops.
	if len(rules) != 2 {
		t.Fatalf("want 2 rules, got %d", len(rules))
	}
	if len(seen) != 1 {
		t.Fatalf("a short first page must end the walk, got %d requests: %v", len(seen), seen)
	}
	if !strings.Contains(seen[0], "page%5Bsize%5D=100") {
		t.Errorf("the paging helper must ask for a page, not the default: %s", seen[0])
	}
}

func TestPreviewAutodiscoveryRule(t *testing.T) {
	var seen []string
	c := newAutodiscSurfaceFixture(t, &seen)
	p, err := c.PreviewAutodiscoveryRule(context.Background(), "ad-aaa")
	if err != nil {
		t.Fatal(err)
	}
	if p.Ref != "release/v1" {
		t.Errorf("ref: %q — the rule's own branch, not a default", p.Ref)
	}
	if p.FilesWalked != 31 {
		t.Errorf("files-walked: %d", p.FilesWalked)
	}
	if len(p.Entries) != 2 {
		t.Fatalf("want 2 entries, got %d", len(p.Entries))
	}
	// The entries are snake_case on the wire; this is what proves the
	// tags match the server rather than the enclosing document's style.
	if p.Entries[0].WorkspaceName != "svc-a" || p.Entries[0].WorkingDirectory != "services/a" {
		t.Errorf("entry 0 not parsed: %+v", p.Entries[0])
	}
	if !p.Entries[0].ExistingAutodiscovered {
		t.Error("existing_autodiscovered must be read: an already-created workspace is not a new one")
	}
	if !p.Entries[1].Collision {
		t.Error("collision must be read: a name taken by a hand-made workspace is skipped, not taken over")
	}
	if seen[0] != "GET /api/v1/autodiscovery-rules/ad-aaa/preview" {
		t.Errorf("wrong path: %s", seen[0])
	}
}

func TestPreviewUnsavedAutodiscoveryRule(t *testing.T) {
	var seen []string
	c := newAutodiscSurfaceFixture(t, &seen)
	p, err := c.PreviewUnsavedAutodiscoveryRule(context.Background(), map[string]any{
		"pattern": "services/*",
	})
	if err != nil {
		t.Fatal(err)
	}
	if p.Ref != "main" || p.FilesWalked != 12 || len(p.Entries) != 1 {
		t.Errorf("unsaved preview not parsed: %+v", p)
	}
	// It must POST the collection's /preview, never the saved rule's —
	// the two differ only by whether a rule exists.
	if seen[0] != "POST /api/v1/autodiscovery-rules/preview" {
		t.Errorf("wrong path: %s", seen[0])
	}
}

func TestScanAutodiscoveryRule(t *testing.T) {
	var seen []string
	c := newAutodiscSurfaceFixture(t, &seen)
	s, err := c.ScanAutodiscoveryRule(context.Background(), "ad-aaa")
	if err != nil {
		t.Fatal(err)
	}
	if s.WorkspacesCreated != 2 || len(s.WorkspaceIDs) != 2 {
		t.Fatalf("scan not parsed: %+v", s)
	}
	if s.WorkspaceIDs[0] != "ws-111" {
		t.Errorf("workspace ids: %v", s.WorkspaceIDs)
	}
	if s.Ref != "main" || s.FilesWalked != 31 {
		t.Errorf("scan metadata: %+v", s)
	}
	if seen[0] != "POST /api/v1/autodiscovery-rules/ad-aaa/scan" {
		t.Errorf("wrong path: %s", seen[0])
	}
}

func TestTheNewAutodiscoverySurfaceSurfacesErrors(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"errors":[{"detail":"platform admin required","status":"403"}]}`))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	// Every one of the four, because an error path that is handled on
	// three of four methods is the one that reaches a caller.
	if _, err := c.ListAutodiscoveryRules(ctx); err == nil {
		t.Error("list: want an error on 403")
	}
	if _, err := c.ListAllAutodiscoveryRules(ctx); err == nil {
		t.Error("list-all: want an error on 403")
	}
	if _, err := c.PreviewAutodiscoveryRule(ctx, "ad-aaa"); err == nil {
		t.Error("preview: want an error on 403")
	}
	if _, err := c.PreviewUnsavedAutodiscoveryRule(ctx, map[string]any{}); err == nil {
		t.Error("unsaved preview: want an error on 403")
	}
	if _, err := c.ScanAutodiscoveryRule(ctx, "ad-aaa"); err == nil {
		t.Error("scan: want an error on 403")
	}
}
