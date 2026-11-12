package terrapod

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

// TestWarmPackagesSendsTheWholeSpec pins that every field a caller sets reaches
// the wire. `source` and `platforms` are the two that cannot be inferred from
// anything else in the request, so a struct tag typo on either would silently
// warm from the wrong place or on the wrong architecture.
func TestWarmPackagesSendsTheWholeSpec(t *testing.T) {
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/v1/admin/package-cache/warm" {
			http.Error(w, "unhandled", http.StatusNotFound)
			return
		}
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &got)
		w.WriteHeader(http.StatusAccepted)
		_, _ = w.Write([]byte(`{"data":{"type":"warm-jobs","id":"warm-abc",
		  "attributes":{"status":"queued","total":2}}}`))
	}))
	t.Cleanup(srv.Close)

	sub, err := newCacheClient(t, srv).WarmPackages(context.Background(), []PackageWarmSpec{
		{Ecosystem: "galaxy", Name: "community.general", Version: "9.2.0", Source: "hub"},
		{Ecosystem: "pulumi", Name: "aws", Version: "6.0.0", Platforms: []string{"linux/arm64"}},
	})
	if err != nil {
		t.Fatal(err)
	}

	if sub.JobID != "warm-abc" || sub.Status != "queued" || sub.Total != 2 {
		t.Fatalf("submission = %+v", sub)
	}
	packages, _ := got["packages"].([]any)
	if len(packages) != 2 {
		t.Fatalf("packages = %v", got["packages"])
	}
	first, _ := packages[0].(map[string]any)
	if first["source"] != "hub" || first["version"] != "9.2.0" {
		t.Errorf("first spec lost a field: %v", first)
	}
	second, _ := packages[1].(map[string]any)
	platforms, _ := second["platforms"].([]any)
	if len(platforms) != 1 || platforms[0] != "linux/arm64" {
		t.Errorf("platforms = %v", second["platforms"])
	}
}

// TestWarmPackagesRefusesAnEmptyList keeps a pointless round trip out of the
// server, and gives the caller the same error the server would.
func TestWarmPackagesRefusesAnEmptyList(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Error("should not have reached the server")
	}))
	t.Cleanup(srv.Close)

	if _, err := newCacheClient(t, srv).WarmPackages(context.Background(), nil); err == nil {
		t.Fatal("want an error for an empty submission")
	}
	if _, err := newCacheClient(t, srv).WarmImages(context.Background(), nil); err == nil {
		t.Fatal("want an error for an empty image list")
	}
}

// TestWarmOnASealedNodeIsAConflict is the refusal that matters most, because
// of when it is read. "0 succeeded" on a sealed node looks exactly like a set
// of missing packages, which sends an operator looking upstream for a problem
// on this side.
func TestWarmOnASealedNodeIsAConflict(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"errors":[{"status":"409","detail":"This node is sealed"}],
		  "detail":"This node is sealed"}`))
	}))
	t.Cleanup(srv.Close)

	_, err := newCacheClient(t, srv).WarmPackages(context.Background(), []PackageWarmSpec{
		{Ecosystem: "pypi", Name: "requests"},
	})
	var conflict *ConflictError
	if !errors.As(err, &conflict) {
		t.Fatalf("want *ConflictError, got %T: %v", err, err)
	}
}

// TestWarmImagesPostsTheReferences checks the second submission route, whose
// body shape differs from the package one.
func TestWarmImagesPostsTheReferences(t *testing.T) {
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/admin/oci/warm" {
			http.Error(w, "unhandled", http.StatusNotFound)
			return
		}
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &got)
		w.WriteHeader(http.StatusAccepted)
		_, _ = w.Write([]byte(`{"data":{"id":"warm-img","attributes":{"status":"queued","total":1}}}`))
	}))
	t.Cleanup(srv.Close)

	sub, err := newCacheClient(t, srv).WarmImages(context.Background(), []string{"quay.io/a/b:1"})
	if err != nil {
		t.Fatal(err)
	}
	if sub.JobID != "warm-img" {
		t.Fatalf("job id = %q", sub.JobID)
	}
	images, _ := got["images"].([]any)
	if len(images) != 1 || images[0] != "quay.io/a/b:1" {
		t.Errorf("images = %v", got["images"])
	}
}

// TestWarmJobStatusKeepsAlreadyCachedDistinct is the property the whole
// `fetched` field exists for: warming is idempotent, so a re-run is the normal
// way to close the last gaps, and without this a converged run is
// indistinguishable from a first one.
func TestWarmJobStatusKeepsAlreadyCachedDistinct(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/api/v1/admin/warm-jobs/warm-abc" {
			http.Error(w, "unhandled", http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":{"type":"warm-jobs","id":"warm-abc","attributes":{
		  "status":"finished","total":3,"completed":3,"succeeded":2,"failed":1,
		  "submitted-at":"2026-01-01T00:00:00Z","outcomes":[
		    {"ecosystem":"pypi","ref":"requests@2.32.3","ok":true,"detail":"","files":4,
		     "fetched":0,"source":"https://hub.example/pypi"},
		    {"ecosystem":"galaxy","ref":"community.general@9.2.0","ok":true,"detail":"",
		     "files":4,"fetched":4,"source":""},
		    {"ecosystem":"npm","ref":"nope","ok":false,"detail":"404 from upstream",
		     "files":0,"fetched":0,"source":""}]}}}`))
	}))
	t.Cleanup(srv.Close)

	job, err := newCacheClient(t, srv).WarmJobStatus(context.Background(), "warm-abc")
	if err != nil {
		t.Fatal(err)
	}

	if !job.Finished() || job.Succeeded != 2 || job.Failed != 1 {
		t.Fatalf("job = %+v", job)
	}
	if len(job.Outcomes) != 3 {
		t.Fatalf("outcomes = %d", len(job.Outcomes))
	}
	already := job.Outcomes[0]
	if already.Files == 0 || already.Fetched != 0 {
		t.Errorf("an already-cached item must carry files with no fetches: %+v", already)
	}
	if already.Source != "https://hub.example/pypi" {
		t.Errorf("source = %q", already.Source)
	}
	fresh := job.Outcomes[1]
	if fresh.Fetched != fresh.Files {
		t.Errorf("a cold item should have fetched everything it covers: %+v", fresh)
	}
	if job.Outcomes[2].Detail != "404 from upstream" {
		t.Errorf("per-item detail lost: %+v", job.Outcomes[2])
	}
}

func TestWarmJobStatusRefusesAnEmptyID(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Error("should not have reached the server")
	}))
	t.Cleanup(srv.Close)

	if _, err := newCacheClient(t, srv).WarmJobStatus(context.Background(), ""); err == nil {
		t.Fatal("want an error for an empty job id")
	}
}

func TestWarmJobStatusNotFound(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"detail":"No such warm job: warm-gone"}`))
	}))
	t.Cleanup(srv.Close)

	_, err := newCacheClient(t, srv).WarmJobStatus(context.Background(), "warm-gone")
	var nf *NotFoundError
	if !errors.As(err, &nf) {
		t.Fatalf("want *NotFoundError, got %T: %v", err, err)
	}
}

// TestPackageCacheSources is the "before it happens" half: which upstream a
// submission would reach, and whether warming is possible at all.
func TestPackageCacheSources(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/api/v1/admin/package-cache/sources" {
			http.Error(w, "unhandled", http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":[
		  {"type":"package-cache-sources","id":"galaxy","attributes":{
		     "ecosystem":"galaxy","enabled":true,"upstream":"https://galaxy.ansible.com",
		     "mirrors":{"hub":"https://hub.example/galaxy"},"resolves-latest":true}},
		  {"type":"package-cache-sources","id":"pulumi","attributes":{
		     "ecosystem":"pulumi","enabled":true,"upstream":"https://get.pulumi.com",
		     "mirrors":{},"resolves-latest":false}}
		],"meta":{"max-items":500,"sealed":true}}`))
	}))
	t.Cleanup(srv.Close)

	sources, meta, err := newCacheClient(t, srv).PackageCacheSources(context.Background())
	if err != nil {
		t.Fatal(err)
	}

	if len(sources) != 2 {
		t.Fatalf("sources = %d", len(sources))
	}
	if sources[0].Mirrors["hub"] != "https://hub.example/galaxy" {
		t.Errorf("mirrors = %v", sources[0].Mirrors)
	}
	// Whether a version may be omitted is a property of the ecosystem's own
	// protocol, so a caller reads it from here rather than hardcoding it.
	if sources[1].ResolvesLatest {
		t.Error("pulumi has no index, so it cannot resolve a latest")
	}
	if meta.MaxItems != MaxWarmItems || !meta.Sealed {
		t.Errorf("meta = %+v", meta)
	}
}
