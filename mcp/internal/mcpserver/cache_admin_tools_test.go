package mcpserver

import (
	"context"
	"io"
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

// readTools and evictTools are the decision, split by ANNOTATION rather than by
// a hand-kept flag: a host decides whether to ask the user from
// DestructiveHint, so the annotation is the authoritative statement of which a
// tool is and this test reads it from the live server.
var (
	readTools = map[string]bool{
		"terrapod_package_cache_list":     true,
		"terrapod_vcs_archive_cache_list": true,
	}
	evictTools = map[string]bool{
		"terrapod_package_cache_delete":    true,
		"terrapod_package_cache_evict":     true,
		"terrapod_vcs_archive_delete":      true,
		"terrapod_vcs_archive_cache_evict": true,
	}
)

// TestCacheAdminRegistersEveryRoute is the decision, asserted rather than
// written down: the server side of #2474 ships eight routes and this group now
// offers all six that are not a totals endpoint (each listing carries its own
// totals).
//
// It replaces a test that pinned the OPPOSITE decision -- two tools, the four
// evictions deliberately absent "because an eviction's only result is a slower
// run later for somebody who did not ask". That reason is a property of the
// OPERATION rather than of the caller, which is the shape AGENTS.md forbids,
// and it was withdrawn in #2494: MCP is not an authorization boundary, so
// withholding a route protected nobody and produced only an asymmetry where an
// agent could read that a cache had grown and not act on it.
//
// Derived from the live server rather than from a list of names, so a tool
// added here tomorrow has to justify itself against this test.
func TestCacheAdminRegistersEveryRoute(t *testing.T) {
	sess := cacheToolCaller(t, func(http.ResponseWriter, *http.Request) {})
	res, err := sess.ListTools(context.Background(), nil)
	if err != nil {
		t.Fatalf("list tools: %v", err)
	}
	var names []string
	for _, tool := range res.Tools {
		names = append(names, tool.Name)
		destructive := tool.Annotations != nil &&
			tool.Annotations.DestructiveHint != nil &&
			*tool.Annotations.DestructiveHint
		readOnlyHint := tool.Annotations != nil && tool.Annotations.ReadOnlyHint

		switch {
		case readTools[tool.Name]:
			if !readOnlyHint {
				t.Errorf("%s is a listing and is not annotated read-only", tool.Name)
			}
			if destructive {
				t.Errorf("%s is a listing and is annotated destructive", tool.Name)
			}
		case evictTools[tool.Name]:
			// BOTH halves. A mutating tool annotated read-only is
			// indistinguishable from a listing to a host deciding whether to
			// ask the operator first, which is the whole mechanism by which
			// risk is signalled now that absence is not.
			if !destructive {
				t.Errorf("%s removes things and is not annotated destructive", tool.Name)
			}
			if readOnlyHint {
				t.Errorf("%s is annotated read-only and it deletes", tool.Name)
			}
		default:
			t.Errorf("%s is neither a listing nor an eviction; classify it here", tool.Name)
		}
	}
	sort.Strings(names)
	want := []string{
		"terrapod_package_cache_delete",
		"terrapod_package_cache_evict",
		"terrapod_package_cache_list",
		"terrapod_vcs_archive_cache_evict",
		"terrapod_vcs_archive_cache_list",
		"terrapod_vcs_archive_delete",
	}
	if strings.Join(names, ",") != strings.Join(want, ",") {
		t.Errorf("registered tools = %v;\n                  want %v", names, want)
	}
}

// TestTheWithdrawnOmissionReasonIsGoneFromEveryDescription bans the SENTENCE,
// not just the behaviour.
//
// The reason #2494 withdrew was carried in three places -- this file's own
// package comment, llms.txt and docs/mcp.md -- and the tool-level Description
// is the one gated by nothing: tool_catalogue.json freezes each tool's name and
// input_schema (so a FIELD description is gated) and nothing at all pins the
// tool-level prose. So a correct set of tools can ship beside a description
// still telling an agent the eviction is not available to it, and no gate
// would say so.
//
// Phrased as whole assertions rather than fragments, so a description may
// legitimately still discuss what an eviction costs -- which every one of them
// does, because that was the true half of the withdrawn reason and it belongs
// in the prose an agent reads.
func TestTheWithdrawnOmissionReasonIsGoneFromEveryDescription(t *testing.T) {
	withdrawn := []string{
		"deliberately not offered as tools",
		"those are deliberately not offered as tools",
		"which is an operator's call to make rather than an agent's",
		"so it is an operator's call",
		"no business being an autonomous one",
	}
	sess := cacheToolCaller(t, func(http.ResponseWriter, *http.Request) {})
	res, err := sess.ListTools(context.Background(), nil)
	if err != nil {
		t.Fatalf("list tools: %v", err)
	}
	for _, tool := range res.Tools {
		for _, phrase := range withdrawn {
			if strings.Contains(tool.Description, phrase) {
				t.Errorf("%s still carries the withdrawn omission reason: %q", tool.Name, phrase)
			}
		}
	}
}

// TestEveryEvictionDescriptionCarriesItsThreeWARNINGS is the positive half: the
// annotation makes a host ASK, and only the description can say what to ask
// about. Each of the three facts here is one an agent cannot infer and would
// otherwise report a confidently wrong answer about.
func TestEveryEvictionDescriptionCarriesItsThreeWarnings(t *testing.T) {
	sess := cacheToolCaller(t, func(http.ResponseWriter, *http.Request) {})
	res, err := sess.ListTools(context.Background(), nil)
	if err != nil {
		t.Fatalf("list tools: %v", err)
	}
	for _, tool := range res.Tools {
		if !evictTools[tool.Name] {
			continue
		}
		// (1) the cost arrives later and lands on a third party, so the agent
		// reports and proposes rather than purging on its own initiative. This
		// is the true half of the withdrawn reason, kept where it is read.
		if !strings.Contains(tool.Description, "later") {
			t.Errorf("%s does not say the cost of an eviction arrives later", tool.Name)
		}
		// (2) a sealed node cannot re-fetch, so the eviction is permanent.
		// Without this an agent recommends a purge as reversible on exactly
		// the deployment where it is not.
		if !strings.Contains(tool.Description, "sealed") {
			t.Errorf("%s does not warn about a sealed node", tool.Name)
		}
		// (3) requires platform admin, so a refusal is RBAC rather than a bug.
		if !strings.Contains(tool.Description, "platform admin") {
			t.Errorf("%s does not say it needs platform admin", tool.Name)
		}
	}
	// And the BULK ones have to say they are bounded, which the single deletes
	// are not. A bounded sweep reported as a finished one is #2491 itself.
	for _, tool := range res.Tools {
		if tool.Name != "terrapod_package_cache_evict" && tool.Name != "terrapod_vcs_archive_cache_evict" {
			continue
		}
		if !strings.Contains(tool.Description, "truncated") {
			t.Errorf("%s does not tell an agent to read `truncated`", tool.Name)
		}
	}
	// Only the ARCHIVE sweep has a cursor to resume from, and only its
	// description may promise one: telling an agent to pass a cursor back to
	// the package eviction would have it loop on a field that is always empty.
	for _, tool := range res.Tools {
		mentions := strings.Contains(tool.Description, "next_cursor")
		if tool.Name == "terrapod_vcs_archive_cache_evict" && !mentions {
			t.Error("the archive sweep does not tell an agent how to resume")
		}
		if tool.Name == "terrapod_package_cache_evict" && mentions {
			t.Error("the package sweep promises a cursor it never returns")
		}
	}
}

func TestPackageCacheEvictToolPostsTheFiltersAndReportsTruncation(t *testing.T) {
	var method, path string
	var body []byte
	sess := cacheToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		method, path = r.Method, r.URL.Path
		body, _ = io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"evicted","files":1000,"bytes":5242880,
		  "truncated":true,"sealed":true,"warning":"This node is sealed."}`))
	})
	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name:      "terrapod_package_cache_evict",
		Arguments: map[string]any{"ecosystem": "npm", "older_than_days": 30},
	})
	if err != nil || res.IsError {
		t.Fatalf("call: %v %s", err, resultText(t, res))
	}
	if method != http.MethodPost || path != "/api/v1/admin/package-cache/evict" {
		t.Errorf("reached %s %s", method, path)
	}
	for _, want := range []string{`"ecosystem":"npm"`, `"older_than_days":30`} {
		if !strings.Contains(string(body), want) {
			t.Errorf("request body missing %s: %s", want, body)
		}
	}
	out := resultText(t, res)
	for _, want := range []string{
		`"files":1000`, `"bytes":5242880`,
		// The honest half. Without it an agent reports a bounded sweep as the
		// whole purge (#2491).
		`"truncated":true`,
		// And the sealed warning, carried verbatim so one wording serves every
		// consumer.
		`"sealed":true`, `"This node is sealed."`,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("result missing %s:\n%s", want, out)
		}
	}
	// The package cache resumes by itself, so there is no cursor to surface --
	// and surfacing an always-empty one would invite an agent to read its
	// absence as the end of the sweep when `truncated` is what says so.
	if strings.Contains(out, "next_cursor") {
		t.Errorf("the package eviction reported a cursor it does not have:\n%s", out)
	}
}

// TestPackageCacheEvictToolSendsZeroDaysRatherThanDroppingIt is the one input
// that cannot be a plain int: `older_than_days: 0` means EVERYTHING past zero
// days, where omitting the field means no age filter at all. An `int` with
// `omitempty` would silently turn the first into the second -- widening the
// eviction from "everything" to "everything", by luck here, and in the other
// direction on any field where 0 is the restrictive value.
func TestPackageCacheEvictToolSendsZeroDaysRatherThanDroppingIt(t *testing.T) {
	var body []byte
	sess := cacheToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		body, _ = io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"evicted","files":0,"bytes":0,"truncated":false}`))
	})
	if _, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name:      "terrapod_package_cache_evict",
		Arguments: map[string]any{"older_than_days": 0},
	}); err != nil {
		t.Fatalf("call: %v", err)
	}
	if !strings.Contains(string(body), `"older_than_days":0`) {
		t.Errorf("zero days was dropped from the request: %s", body)
	}
}

func TestVCSArchiveCacheEvictToolRoundTripsTheCursor(t *testing.T) {
	var method, path string
	var body []byte
	sess := cacheToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		method, path = r.Method, r.URL.Path
		body, _ = io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"evicted","archives":406,"bytes":1178927104,
		  "truncated":true,"next-cursor":"vcs_archives/c1/o/r/zz-full.tar.gz","sealed":false}`))
	})
	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "terrapod_vcs_archive_cache_evict",
		Arguments: map[string]any{
			"connection_id":   "vcs-1b3f",
			"older_than_days": 7,
			"after":           "vcs_archives/c1/o/r/aa-full.tar.gz",
		},
	})
	if err != nil || res.IsError {
		t.Fatalf("call: %v %s", err, resultText(t, res))
	}
	if method != http.MethodPost || path != "/api/v1/admin/vcs-archive-cache/evict" {
		t.Errorf("reached %s %s", method, path)
	}
	// The cursor has to travel OUTBOUND as well as back, or a sweep cannot be
	// resumed and the window restarts at the same place every call -- which is
	// the defect that made a large archive cache unpurgeable (#2491).
	for _, want := range []string{
		`"connection_id":"vcs-1b3f"`, `"older_than_days":7`,
		`"after":"vcs_archives/c1/o/r/aa-full.tar.gz"`,
	} {
		if !strings.Contains(string(body), want) {
			t.Errorf("request body missing %s: %s", want, body)
		}
	}
	out := resultText(t, res)
	for _, want := range []string{
		// The server spells the count `archives`; it lands in `files`, the
		// same fold the SDK does, because an archive is one file.
		`"files":406`, `"bytes":1178927104`, `"truncated":true`,
		`"next_cursor":"vcs_archives/c1/o/r/zz-full.tar.gz"`,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("result missing %s:\n%s", want, out)
		}
	}
}

func TestTheSingleDeleteToolsReachTheirRoutes(t *testing.T) {
	cases := []struct {
		tool, arg, value, wantPath, wantQuery string
	}{
		{
			tool: "terrapod_package_cache_delete", arg: "file_id", value: "pf-1",
			wantPath: "/api/v1/admin/package-cache/pf-1",
		},
		{
			// The key is a QUERY parameter, not a path segment: it contains
			// slashes, and a path would have to encode them.
			tool: "terrapod_vcs_archive_delete", arg: "key",
			value:     "vcs_archives/c1/o/r/aa-full.tar.gz",
			wantPath:  "/api/v1/admin/vcs-archive-cache",
			wantQuery: "key=vcs_archives%2Fc1%2Fo%2Fr%2Faa-full.tar.gz",
		},
	}
	for _, tc := range cases {
		t.Run(tc.tool, func(t *testing.T) {
			var method, path, query string
			sess := cacheToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
				method, path, query = r.Method, r.URL.Path, r.URL.RawQuery
				w.Header().Set("Content-Type", "application/json")
				_, _ = w.Write([]byte(`{"status":"evicted"}`))
			})
			res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
				Name:      tc.tool,
				Arguments: map[string]any{tc.arg: tc.value},
			})
			if err != nil || res.IsError {
				t.Fatalf("call: %v %s", err, resultText(t, res))
			}
			if method != http.MethodDelete || path != tc.wantPath {
				t.Errorf("reached %s %s; want DELETE %s", method, path, tc.wantPath)
			}
			if tc.wantQuery != "" && query != tc.wantQuery {
				t.Errorf("query = %q; want %q", query, tc.wantQuery)
			}
			if out := resultText(t, res); !strings.Contains(out, `"deleted":true`) {
				t.Errorf("result did not confirm the deletion:\n%s", out)
			}
		})
	}
}

// TestTheEvictionToolsRefuseAnEmptyIdentifierWithoutCallingTheAPI: a destructive
// tool handed nothing must not reach the server at all. Without the guard the
// package delete would DELETE /api/v1/admin/package-cache/ -- which is the
// LISTING's path with a trailing slash, not a delete of nothing.
func TestTheEvictionToolsRefuseAnEmptyIdentifierWithoutCallingTheAPI(t *testing.T) {
	for _, tc := range []struct{ tool, arg string }{
		{"terrapod_package_cache_delete", "file_id"},
		{"terrapod_vcs_archive_delete", "key"},
	} {
		t.Run(tc.tool, func(t *testing.T) {
			called := false
			sess := cacheToolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
				called = true
				w.Header().Set("Content-Type", "application/json")
				_, _ = w.Write([]byte(`{"status":"evicted"}`))
			})
			res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
				Name:      tc.tool,
				Arguments: map[string]any{tc.arg: ""},
			})
			if err != nil {
				t.Fatalf("call: %v", err)
			}
			if !res.IsError {
				t.Errorf("an empty %s was accepted", tc.arg)
			}
			if called {
				t.Errorf("an empty %s reached the API", tc.arg)
			}
		})
	}
}

func TestTheEvictionToolsSurfaceAnError(t *testing.T) {
	for _, tc := range []struct {
		tool string
		args map[string]any
	}{
		{"terrapod_package_cache_evict", map[string]any{"ecosystem": "npm"}},
		{"terrapod_vcs_archive_cache_evict", map[string]any{"connection_id": "c1"}},
		{"terrapod_package_cache_delete", map[string]any{"file_id": "pf-1"}},
		{"terrapod_vcs_archive_delete", map[string]any{"key": "vcs_archives/a/b/c/d-full.tar.gz"}},
	} {
		t.Run(tc.tool, func(t *testing.T) {
			sess := cacheToolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusForbidden)
				_, _ = w.Write([]byte(`{"errors":[{"detail":"admin required","status":"403"}]}`))
			})
			res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
				Name: tc.tool, Arguments: tc.args,
			})
			if err != nil {
				t.Fatalf("call: %v", err)
			}
			if !res.IsError {
				t.Fatalf("a 403 was not surfaced as an error")
			}
			if out := resultText(t, res); !strings.Contains(out, "admin required") {
				t.Errorf("the server's own reason was dropped:\n%s", out)
			}
		})
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
