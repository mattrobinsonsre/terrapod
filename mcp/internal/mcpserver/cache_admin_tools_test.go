package mcpserver

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sort"
	"strings"
	"testing"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// The cache-admin read tools (#2474) driven end to end: a real MCP client
// against a real server, backed by a fake Terrapod. The catalogue golden pins
// registration and the schemas; these pin what each tool asks the API for and
// what it surfaces — including the two places where getting it wrong produces
// a confidently wrong answer rather than an error.

// cacheToolCaller is toolCaller's sibling for this group: the shared one
// registers Observe/Act/Ground only, so it cannot see these tools at all.
func cacheToolCaller(t *testing.T, handler http.HandlerFunc) *mcp.ClientSession {
	t.Helper()
	api := httptest.NewServer(handler)
	t.Cleanup(api.Close)

	c, err := terrapod.NewClient(terrapod.Options{BaseURL: api.URL, Token: "t"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	srv := mcp.NewServer(&mcp.Implementation{Name: "test", Version: "0"}, nil)
	registerCacheAdmin(srv, c)

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

// TestCacheAdminRegistersReadsOnly is the decision, asserted rather than
// written down: the server side of #2474 ships eight routes and this group
// offers the two listings. An eviction's only result is a slower run later for
// somebody who did not ask, so it is an operator's action, not an agent's.
//
// Derived from the live server rather than from a list of names, so a tool
// added here tomorrow has to justify itself against this test.
func TestCacheAdminRegistersReadsOnly(t *testing.T) {
	sess := cacheToolCaller(t, func(http.ResponseWriter, *http.Request) {})
	res, err := sess.ListTools(context.Background(), nil)
	if err != nil {
		t.Fatalf("list tools: %v", err)
	}
	var names []string
	for _, tool := range res.Tools {
		names = append(names, tool.Name)
		if tool.Annotations == nil || !tool.Annotations.ReadOnlyHint {
			t.Errorf("%s is not annotated read-only; this group reads and nothing else", tool.Name)
		}
		if tool.Annotations != nil && tool.Annotations.DestructiveHint != nil && *tool.Annotations.DestructiveHint {
			t.Errorf("%s is annotated destructive, so it is not a read", tool.Name)
		}
	}
	sort.Strings(names)
	want := "terrapod_package_cache_list,terrapod_vcs_archive_cache_list"
	if strings.Join(names, ",") != want {
		t.Errorf("registered tools = %v; want exactly %s (no evict/delete tool belongs here)", names, want)
	}
	// Each description has to say WHY the eviction is absent, or the next
	// reader reads the gap as an oversight and fills it.
	for _, tool := range res.Tools {
		if !strings.Contains(tool.Description, "evict") {
			t.Errorf("%s does not explain why the eviction routes are not tools", tool.Name)
		}
	}
}

func TestPackageCacheListToolReadsTheListAndTheTotals(t *testing.T) {
	var paths, queries []string
	sess := cacheToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.URL.Path)
		queries = append(queries, r.URL.RawQuery)
		w.Header().Set("Content-Type", "application/json")
		if strings.HasSuffix(r.URL.Path, "/totals") {
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"galaxy","type":"package-cache-totals","attributes":{"ecosystem":"galaxy","files":9,"bytes":90000}},
			  {"id":"npm","type":"package-cache-totals","attributes":{"ecosystem":"npm","files":1,"bytes":10}}
			],"meta":{"files":10,"bytes":90010}}`))
			return
		}
		_, _ = w.Write([]byte(`{"data":[
		  {"id":"pf-1","type":"cached-package-files","attributes":{
		     "ecosystem":"galaxy","name":"community.general","version":"9.0.0",
		     "filename":"community-general-9.0.0.tar.gz","size":4096,"digest":"sha256:abc",
		     "cached-at":"2026-01-01T00:00:00Z","last-accessed-at":"2026-02-01T00:00:00Z"}}
		],"meta":{"pagination":{"current-page":1,"page-size":50,"total-count":3,"total-pages":1}}}`))
	})

	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name:      "terrapod_package_cache_list",
		Arguments: map[string]any{"ecosystem": "galaxy", "name": "community."},
	})
	if err != nil || res.IsError {
		t.Fatalf("call: %v %s", err, resultText(t, res))
	}
	// Both endpoints: the totals are the half that stays correct when the page
	// is short, so a tool that only listed would understate the cache.
	joined := strings.Join(paths, " ")
	if !strings.Contains(joined, "/api/v1/admin/package-cache") || !strings.Contains(joined, "/api/v1/admin/package-cache/totals") {
		t.Fatalf("paths = %v", paths)
	}
	if !strings.Contains(queries[0], "ecosystem=galaxy") || !strings.Contains(queries[0], "name=community.") {
		t.Errorf("filters did not reach the wire: %q", queries[0])
	}
	out := resultText(t, res)
	for _, want := range []string{
		`"community.general"`,
		`"digest":"sha256:abc"`,
		`"last_accessed_at":"2026-02-01T00:00:00Z"`,
		// The whole-cache total, which is larger than the page on purpose.
		`"files":10`, `"bytes":90010`,
		`"by_ecosystem"`,
		// One row returned against a total of three is a truncated page, and
		// an agent that cannot see that reports a third of the cache as all
		// of it.
		`"returned":1`, `"total":3`, `"truncated":true`,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("result missing %s:\n%s", want, out)
		}
	}
}

func TestPackageCacheListToolSurfacesAnError(t *testing.T) {
	sess := cacheToolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"errors":[{"status":"403","detail":"admin required"}]}`))
	})
	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "terrapod_package_cache_list", Arguments: map[string]any{},
	})
	if err != nil {
		t.Fatalf("call: %v", err)
	}
	if !res.IsError {
		t.Fatalf("a 403 should reach the agent as a tool error, got %s", resultText(t, res))
	}
	// The RBAC case has its own wording: re-login does not help, an admin
	// must grant the capability.
	if !strings.Contains(resultText(t, res), "permission denied") {
		t.Errorf("error text = %s", resultText(t, res))
	}
}

func TestVCSArchiveCacheListToolFirstPageCarriesTheTotals(t *testing.T) {
	var paths []string
	sess := cacheToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.URL.Path)
		w.Header().Set("Content-Type", "application/json")
		if strings.HasSuffix(r.URL.Path, "/totals") {
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"c1/acme/app","type":"vcs-archive-cache-totals","attributes":{
			     "connection-id":"c1","owner":"acme","repo":"app","archives":40,"bytes":80000}}
			],"meta":{"archives":40,"bytes":80000,"truncated":true,"scan-limit":5000}}`))
			return
		}
		_, _ = w.Write([]byte(`{"data":[
		  {"id":"vcs_archives/c1/acme/app/abc123-full.tar.gz","type":"cached-vcs-archives","attributes":{
		     "key":"vcs_archives/c1/acme/app/abc123-full.tar.gz","connection-id":"c1","owner":"acme",
		     "repo":"app","sha":"abc123","paths-hash":"full","size":2048,
		     "last-modified":"2026-03-01T00:00:00Z"}}
		],"meta":{"next-cursor":"vcs_archives/c1/acme/app/abc123-full.tar.gz","page-size":1}}`))
	})

	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "terrapod_vcs_archive_cache_list", Arguments: map[string]any{},
	})
	if err != nil || res.IsError {
		t.Fatalf("call: %v %s", err, resultText(t, res))
	}
	if len(paths) != 2 {
		t.Fatalf("expected the listing and the totals, got %v", paths)
	}
	out := resultText(t, res)
	for _, want := range []string{
		`"paths_hash":"full"`,
		`"last_modified":"2026-03-01T00:00:00Z"`,
		`"next_cursor":"vcs_archives/c1/acme/app/abc123-full.tar.gz"`,
		`"by_repo"`,
		// The flag is the whole reason the totals are worth returning: 40
		// archives presented as the size of the cache, when the walk stopped
		// at 5000 keys, understates exactly what the caller came to find.
		`"truncated":true`, `"scan_limit":5000`,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("result missing %s:\n%s", want, out)
		}
	}
}

func TestVCSArchiveCacheListToolResumesWithoutRewalkingTheTotals(t *testing.T) {
	var paths, queries []string
	sess := cacheToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.URL.Path)
		queries = append(queries, r.URL.RawQuery)
		w.Header().Set("Content-Type", "application/json")
		if strings.HasSuffix(r.URL.Path, "/totals") {
			t.Error("a resumed page must not re-walk storage for a total it already reported")
			_, _ = w.Write([]byte(`{"data":[],"meta":{}}`))
			return
		}
		// The last page: an empty cursor is the only end-of-listing signal.
		_, _ = w.Write([]byte(`{"data":[],"meta":{"next-cursor":"","page-size":0}}`))
	})

	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name:      "terrapod_vcs_archive_cache_list",
		Arguments: map[string]any{"after": "vcs_archives/c1/acme/app/abc123-full.tar.gz", "limit": 25},
	})
	if err != nil || res.IsError {
		t.Fatalf("call: %v %s", err, resultText(t, res))
	}
	if len(paths) != 1 {
		t.Fatalf("a resumed page should be one request, got %v", paths)
	}
	// The cursor has to reach the wire as `after=`, or every page is the first
	// page and the walk never advances.
	if !strings.Contains(queries[0], "after=vcs_archives") {
		t.Errorf("the cursor did not reach the wire: %q", queries[0])
	}
	if !strings.Contains(queries[0], "limit=25") {
		t.Errorf("limit did not reach the wire: %q", queries[0])
	}
	out := resultText(t, res)
	// Null rather than a zeroed object: a totals block reading zero on a
	// resumed page would say the cache is empty, which is the one answer that
	// is worse than no answer.
	if !strings.Contains(out, `"totals":null`) {
		t.Errorf("a resumed page should report totals as null, got:\n%s", out)
	}
	if !strings.Contains(out, `"next_cursor":""`) {
		t.Errorf("the end of the listing should be an empty cursor:\n%s", out)
	}
}
