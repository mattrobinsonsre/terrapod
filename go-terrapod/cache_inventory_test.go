package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// newCacheClient wires a client to srv, failing the test rather than returning
// an error nobody checks.
func newCacheClient(t *testing.T, srv *httptest.Server) *Client {
	t.Helper()
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestListPackageCache(t *testing.T) {
	var gotQuery string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/api/v1/admin/package-cache" {
			http.Error(w, "unhandled", http.StatusNotFound)
			return
		}
		gotQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":[
		  {"id":"pf-1","type":"cached-package-files","attributes":{
		     "ecosystem":"galaxy","name":"community.general","version":"9.0.0",
		     "filename":"community-general-9.0.0.tar.gz","size":4096,
		     "digest":"sha256:abc","cached-at":"2026-01-01T00:00:00Z",
		     "last-accessed-at":"2026-02-01T00:00:00Z"}}
		],"meta":{"pagination":{"current-page":2,"page-size":1,"total-pages":3,"total-count":3}}}`))
	}))
	t.Cleanup(srv.Close)

	list, err := newCacheClient(t, srv).ListPackageCache(context.Background(), PackageCacheListOptions{
		Ecosystem: "galaxy", Name: "community.", PageNumber: 2, PageSize: 1,
	})
	if err != nil {
		t.Fatalf("ListPackageCache: %v", err)
	}
	// Every filter must reach the wire under the name the server reads.
	for _, want := range []string{"ecosystem=galaxy", "name=community.", "page%5Bnumber%5D=2", "page%5Bsize%5D=1"} {
		if !strings.Contains(gotQuery, want) {
			t.Errorf("query %q is missing %q", gotQuery, want)
		}
	}
	if len(list.Items) != 1 {
		t.Fatalf("expected one entry, got %d", len(list.Items))
	}
	got := list.Items[0]
	if got.ID != "pf-1" || got.Ecosystem != "galaxy" || got.Name != "community.general" {
		t.Errorf("identity: %+v", got)
	}
	// Size must survive as an integer: hand-walking a map[string]any is how a
	// numeric attribute quietly becomes a float64.
	if got.Size != 4096 {
		t.Errorf("size: %d", got.Size)
	}
	// The digest is upstream's own and must not be rewritten.
	if got.Digest != "sha256:abc" {
		t.Errorf("digest: %q", got.Digest)
	}
	if got.CachedAt != "2026-01-01T00:00:00Z" || got.LastAccessedAt != "2026-02-01T00:00:00Z" {
		t.Errorf("timestamps: %+v", got)
	}
	if list.TotalPages != 3 || list.TotalCount != 3 || list.CurrentPage != 2 {
		t.Errorf("pagination: %+v", list)
	}
}

func TestListPackageCache_Error(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"errors":[{"status":"403","detail":"admin required"}]}`))
	}))
	t.Cleanup(srv.Close)

	if _, err := newCacheClient(t, srv).ListPackageCache(context.Background(), PackageCacheListOptions{}); err == nil {
		t.Fatal("expected an error on 403")
	}
}

func TestListAllPackageCache_PagesToTheEnd(t *testing.T) {
	var pages []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		page := r.URL.Query().Get("page[number]")
		pages = append(pages, page)
		w.Header().Set("Content-Type", "application/json")
		id := "pf-" + page
		_, _ = fmt.Fprintf(w, `{"data":[{"id":%q,"type":"cached-package-files","attributes":{"ecosystem":"npm","name":"x","version":"1","filename":"x.tgz","size":1,"digest":"d","cached-at":"","last-accessed-at":""}}],
		  "meta":{"pagination":{"current-page":%s,"page-size":1,"total-pages":2,"total-count":2}}}`, id, page)
	}))
	t.Cleanup(srv.Close)

	all, err := newCacheClient(t, srv).ListAllPackageCache(context.Background(), PackageCacheListOptions{PageSize: 1})
	if err != nil {
		t.Fatalf("ListAllPackageCache: %v", err)
	}
	// It must stop on total-pages rather than run forever, and must not skip
	// the last page.
	if len(all) != 2 || all[0].ID != "pf-1" || all[1].ID != "pf-2" {
		t.Fatalf("collected %d entries: %+v", len(all), all)
	}
	if strings.Join(pages, ",") != "1,2" {
		t.Errorf("requested pages %v", pages)
	}
}

func TestPackageCacheTotals(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/admin/package-cache/totals" {
			http.Error(w, "unhandled", http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":[
		  {"id":"npm","type":"package-cache-totals","attributes":{"ecosystem":"npm","files":12,"bytes":3000}},
		  {"id":"pypi","type":"package-cache-totals","attributes":{"ecosystem":"pypi","files":3,"bytes":500}}
		],"meta":{"files":15,"bytes":3500}}`))
	}))
	t.Cleanup(srv.Close)

	totals, meta, err := newCacheClient(t, srv).PackageCacheTotals(context.Background())
	if err != nil {
		t.Fatalf("PackageCacheTotals: %v", err)
	}
	if len(totals) != 2 || totals[0].Ecosystem != "npm" || totals[0].Files != 12 || totals[0].Bytes != 3000 {
		t.Fatalf("totals: %+v", totals)
	}
	// The whole-cache total is the server's aggregate, not a sum of the page --
	// surfacing it is the point of returning the meta at all.
	if meta.Files != 15 || meta.Bytes != 3500 {
		t.Errorf("meta: %+v", meta)
	}
}

func TestPackageCacheTotals_Error(t *testing.T) {
	// A 403 rather than a 5xx: the client retries 5xx with backoff, so a 500
	// fixture spends seven seconds proving something the client's own retry
	// tests already cover. 403 is also the realistic failure on an admin route.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"errors":[{"status":"403","detail":"admin required"}]}`))
	}))
	t.Cleanup(srv.Close)

	if _, _, err := newCacheClient(t, srv).PackageCacheTotals(context.Background()); err == nil {
		t.Fatal("expected an error on 403")
	}
}

func TestDeletePackageCacheFile(t *testing.T) {
	var gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete {
			http.Error(w, "unhandled", http.StatusMethodNotAllowed)
			return
		}
		gotPath = r.URL.Path
		if strings.HasSuffix(gotPath, "/missing") {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"errors":[{"status":"404","detail":"no such cached artifact"}]}`))
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"evicted"}`))
	}))
	t.Cleanup(srv.Close)

	c := newCacheClient(t, srv)
	if err := c.DeletePackageCacheFile(context.Background(), "pf-1"); err != nil {
		t.Fatalf("DeletePackageCacheFile: %v", err)
	}
	if gotPath != "/api/v1/admin/package-cache/pf-1" {
		t.Errorf("path: %q", gotPath)
	}
	// An absent row is a typed 404 so a caller can treat it as success.
	err := c.DeletePackageCacheFile(context.Background(), "missing")
	if err == nil || !IsNotFound(err) {
		t.Fatalf("expected a not-found error, got %v", err)
	}
	// The id is required locally rather than producing a request to a path
	// that would address the collection.
	if err := c.DeletePackageCacheFile(context.Background(), ""); err == nil {
		t.Error("an empty id should be refused before the request")
	}
}

func TestEvictPackageCache(t *testing.T) {
	var body map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/v1/admin/package-cache/evict" {
			http.Error(w, "unhandled", http.StatusNotFound)
			return
		}
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"evicted","files":7,"bytes":900}`))
	}))
	t.Cleanup(srv.Close)

	days := 30
	res, err := newCacheClient(t, srv).EvictPackageCache(context.Background(), PackageEvictRequest{
		Ecosystem: "npm", OlderThanDays: &days,
	})
	if err != nil {
		t.Fatalf("EvictPackageCache: %v", err)
	}
	// snake_case on the wire: this endpoint takes a plain body, not a JSON:API
	// document, and it forbids unknown fields -- so a kebab-case key would be
	// a 422 rather than an ignored filter.
	if body["ecosystem"] != "npm" || body["older_than_days"] != float64(30) {
		t.Errorf("body: %+v", body)
	}
	if res.Files != 7 || res.Bytes != 900 {
		t.Errorf("result: %+v", res)
	}
}

func TestEvictPackageCache_OmitsAnUnsetAgeFilter(t *testing.T) {
	var body map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"evicted","files":0,"bytes":0}`))
	}))
	t.Cleanup(srv.Close)

	if _, err := newCacheClient(t, srv).EvictPackageCache(context.Background(), PackageEvictRequest{}); err != nil {
		t.Fatalf("EvictPackageCache: %v", err)
	}
	// A nil OlderThanDays must be ABSENT, not zero: zero days would evict
	// everything, which is the opposite of "no age filter".
	if _, present := body["older_than_days"]; present {
		t.Errorf("an unset age filter reached the wire: %+v", body)
	}
}

func TestListVCSArchiveCache_Cursor(t *testing.T) {
	var queries []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/admin/vcs-archive-cache" {
			http.Error(w, "unhandled", http.StatusNotFound)
			return
		}
		queries = append(queries, r.URL.RawQuery)
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Query().Get("after") == "" {
			// A full page: the cursor is the last key.
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"vcs_archives/c1/acme/app/abc123-full.tar.gz","type":"cached-vcs-archives","attributes":{
			     "key":"vcs_archives/c1/acme/app/abc123-full.tar.gz","connection-id":"c1","owner":"acme",
			     "repo":"app","sha":"abc123","paths-hash":"full","size":2048,
			     "last-modified":"2026-03-01T00:00:00Z"}}
			],"meta":{"next-cursor":"vcs_archives/c1/acme/app/abc123-full.tar.gz","page-size":1}}`))
			return
		}
		// The last page: an EMPTY cursor is how the caller knows it is done.
		_, _ = w.Write([]byte(`{"data":[],"meta":{"next-cursor":"","page-size":0}}`))
	}))
	t.Cleanup(srv.Close)

	c := newCacheClient(t, srv)
	first, err := c.ListVCSArchiveCache(context.Background(), VCSArchiveListOptions{ConnectionID: "c1", Limit: 1})
	if err != nil {
		t.Fatalf("ListVCSArchiveCache: %v", err)
	}
	if !strings.Contains(queries[0], "connection_id=c1") || !strings.Contains(queries[0], "limit=1") {
		t.Errorf("first query %q", queries[0])
	}
	if strings.Contains(queries[0], "after=") {
		t.Errorf("an empty cursor must not reach the wire: %q", queries[0])
	}
	if len(first.Items) != 1 || first.Items[0].Key != "vcs_archives/c1/acme/app/abc123-full.tar.gz" {
		t.Fatalf("first page: %+v", first.Items)
	}
	got := first.Items[0]
	if got.ConnectionID != "c1" || got.Owner != "acme" || got.Repo != "app" || got.SHA != "abc123" {
		t.Errorf("key parts: %+v", got)
	}
	// "full" means a whole-repo archive rather than a path-narrowed one, so it
	// must survive verbatim rather than being read as a missing hash.
	if got.PathsHash != "full" {
		t.Errorf("paths-hash: %q", got.PathsHash)
	}
	if got.Size != 2048 || got.LastModified != "2026-03-01T00:00:00Z" {
		t.Errorf("size/last-modified: %+v", got)
	}
	if first.NextCursor == "" {
		t.Fatal("a full page must carry a cursor")
	}
	if first.PageSize != 1 {
		t.Errorf("page-size: %d", first.PageSize)
	}

	// The cursor must reach the wire as `after=`.
	second, err := c.ListVCSArchiveCache(context.Background(), VCSArchiveListOptions{After: first.NextCursor})
	if err != nil {
		t.Fatalf("second page: %v", err)
	}
	if !strings.Contains(queries[1], "after=vcs_archives") {
		t.Errorf("second query %q does not carry the cursor", queries[1])
	}
	// An empty cursor, not a short page, is the end-of-listing signal.
	if second.NextCursor != "" {
		t.Errorf("the last page should carry no cursor, got %q", second.NextCursor)
	}
}

func TestListVCSArchiveCache_Error(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"errors":[{"status":"403","detail":"admin required"}]}`))
	}))
	t.Cleanup(srv.Close)

	if _, err := newCacheClient(t, srv).ListVCSArchiveCache(context.Background(), VCSArchiveListOptions{}); err == nil {
		t.Fatal("expected an error on 403")
	}
}

func TestListAllVCSArchiveCache_FollowsTheCursor(t *testing.T) {
	var afters []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		after := r.URL.Query().Get("after")
		afters = append(afters, after)
		// `page[number]` must never appear: this endpoint has no offset paging,
		// and a loop built on one would re-read the first page for ever.
		if r.URL.Query().Get("page[number]") != "" {
			http.Error(w, "this endpoint has no page[number]", http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		switch after {
		case "":
			_, _ = w.Write([]byte(`{"data":[{"id":"k1","type":"cached-vcs-archives","attributes":{"key":"k1","size":1}}],
			  "meta":{"next-cursor":"k1","page-size":1}}`))
		case "k1":
			// Deliberately a FULL final page that still carries a cursor, so
			// the loop has to make one more request to learn it is finished.
			_, _ = w.Write([]byte(`{"data":[{"id":"k2","type":"cached-vcs-archives","attributes":{"key":"k2","size":2}}],
			  "meta":{"next-cursor":"k2","page-size":1}}`))
		default:
			_, _ = w.Write([]byte(`{"data":[],"meta":{"next-cursor":"","page-size":0}}`))
		}
	}))
	t.Cleanup(srv.Close)

	all, err := newCacheClient(t, srv).ListAllVCSArchiveCache(context.Background(), VCSArchiveListOptions{Limit: 1})
	if err != nil {
		t.Fatalf("ListAllVCSArchiveCache: %v", err)
	}
	if len(all) != 2 || all[0].Key != "k1" || all[1].Key != "k2" {
		t.Fatalf("collected %+v", all)
	}
	if strings.Join(afters, ",") != ",k1,k2" {
		t.Errorf("cursors followed: %v", afters)
	}
}

func TestListAllVCSArchiveCache_StopsOnARepeatedCursor(t *testing.T) {
	// Nothing else bounds the cursor loop: a server that hands back a cursor it
	// has already given would hang the caller for ever rather than answer.
	var calls int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls++
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":[{"id":"k1","type":"cached-vcs-archives","attributes":{"key":"k1","size":1}}],
		  "meta":{"next-cursor":"k1","page-size":1}}`))
	}))
	t.Cleanup(srv.Close)

	all, err := newCacheClient(t, srv).ListAllVCSArchiveCache(context.Background(), VCSArchiveListOptions{Limit: 1})
	if err != nil {
		t.Fatalf("ListAllVCSArchiveCache: %v", err)
	}
	// Two requests: the first learns the cursor, the second sees it repeat.
	if calls != 2 {
		t.Errorf("made %d requests; the loop should stop as soon as a cursor repeats", calls)
	}
	if len(all) != 2 {
		t.Errorf("collected %d entries", len(all))
	}
}

func TestVCSArchiveCacheTotals_SurfacesTruncation(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/admin/vcs-archive-cache/totals" {
			http.Error(w, "unhandled", http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":[
		  {"id":"c1/acme/app","type":"vcs-archive-cache-totals","attributes":{
		     "connection-id":"c1","owner":"acme","repo":"app","archives":40,"bytes":80000}}
		],"meta":{"archives":40,"bytes":80000,"truncated":true,"scan-limit":5000}}`))
	}))
	t.Cleanup(srv.Close)

	totals, meta, err := newCacheClient(t, srv).VCSArchiveCacheTotals(context.Background())
	if err != nil {
		t.Fatalf("VCSArchiveCacheTotals: %v", err)
	}
	if len(totals) != 1 || totals[0].Repo != "app" || totals[0].Archives != 40 || totals[0].Bytes != 80000 {
		t.Fatalf("totals: %+v", totals)
	}
	// Truncated and ScanLimit are the whole reason the meta is returned: a
	// caller that cannot see them cannot tell a complete total from a lower
	// bound, and would present an understated number as the answer.
	if !meta.Truncated {
		t.Error("truncated did not survive the decode")
	}
	if meta.ScanLimit != 5000 {
		t.Errorf("scan-limit: %d", meta.ScanLimit)
	}
	if meta.Archives != 40 || meta.Bytes != 80000 {
		t.Errorf("meta totals: %+v", meta)
	}
}

func TestVCSArchiveCacheTotals_Error(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"errors":[{"status":"403","detail":"admin required"}]}`))
	}))
	t.Cleanup(srv.Close)

	if _, _, err := newCacheClient(t, srv).VCSArchiveCacheTotals(context.Background()); err == nil {
		t.Fatal("expected an error on 403")
	}
}

func TestDeleteVCSArchive(t *testing.T) {
	var gotKey, gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete {
			http.Error(w, "unhandled", http.StatusMethodNotAllowed)
			return
		}
		gotPath = r.URL.Path
		gotKey = r.URL.Query().Get("key")
		if !strings.HasPrefix(gotKey, "vcs_archives/") {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusUnprocessableEntity)
			_, _ = w.Write([]byte(`{"errors":[{"status":"422","detail":"key must be a cached VCS archive"}]}`))
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"evicted"}`))
	}))
	t.Cleanup(srv.Close)

	c := newCacheClient(t, srv)
	key := "vcs_archives/c1/acme/app/abc123-full.tar.gz"
	if err := c.DeleteVCSArchive(context.Background(), key); err != nil {
		t.Fatalf("DeleteVCSArchive: %v", err)
	}
	// The key is a QUERY parameter and its slashes must survive the escape --
	// a path segment would have to encode them and the route takes neither.
	if gotPath != "/api/v1/admin/vcs-archive-cache" {
		t.Errorf("path: %q", gotPath)
	}
	if gotKey != key {
		t.Errorf("key did not round-trip: %q", gotKey)
	}
	// A key outside the archive prefix is refused by the server, and the
	// refusal must reach the caller as a validation error rather than as a
	// silent success.
	err := c.DeleteVCSArchive(context.Background(), "state/ws-1/1.tfstate")
	if err == nil || !IsValidation(err) {
		t.Fatalf("expected a validation error, got %v", err)
	}
	if err := c.DeleteVCSArchive(context.Background(), ""); err == nil {
		t.Error("an empty key should be refused before the request")
	}
}

func TestEvictVCSArchiveCache(t *testing.T) {
	var body map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/v1/admin/vcs-archive-cache/evict" {
			http.Error(w, "unhandled", http.StatusNotFound)
			return
		}
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &body)
		w.Header().Set("Content-Type", "application/json")
		// Note the key: this endpoint counts `archives`, where the package one
		// counts `files`.
		_, _ = w.Write([]byte(`{"status":"evicted","archives":4,"bytes":12000}`))
	}))
	t.Cleanup(srv.Close)

	days := 7
	res, err := newCacheClient(t, srv).EvictVCSArchiveCache(context.Background(), ArchiveEvictRequest{
		ConnectionID: "c1", OlderThanDays: &days,
	})
	if err != nil {
		t.Fatalf("EvictVCSArchiveCache: %v", err)
	}
	if body["connection_id"] != "c1" || body["older_than_days"] != float64(7) {
		t.Errorf("body: %+v", body)
	}
	// `archives` must land in Files, or the count reads as zero on a call that
	// removed four archives.
	if res.Files != 4 || res.Bytes != 12000 {
		t.Errorf("result: %+v", res)
	}
}

func TestEvictVCSArchiveCache_Error(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(`{"errors":[{"status":"422","detail":"unknown field"}]}`))
	}))
	t.Cleanup(srv.Close)

	if _, err := newCacheClient(t, srv).EvictVCSArchiveCache(context.Background(), ArchiveEvictRequest{}); err == nil {
		t.Fatal("expected an error on 422")
	}
}
