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

// The cache-warm tools (#2486) driven end to end, against a fake Terrapod.
//
// The catalogue golden pins that they exist and what their schemas are. These
// pin the two things it cannot see: what each tool asks the API for, and
// whether the descriptions tell an agent the things that change what a correct
// call looks like. A tool-level description is frozen by nothing -- the
// catalogue freezes `name` and `input_schema` and stops there -- so a claim in
// one can go false with every gate green.

func warmToolCaller(t *testing.T, handler http.HandlerFunc) *mcp.ClientSession {
	t.Helper()
	api := httptest.NewServer(handler)
	t.Cleanup(api.Close)

	c, err := terrapod.NewClient(terrapod.Options{BaseURL: api.URL, Token: "t"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	srv := mcp.NewServer(&mcp.Implementation{Name: "test", Version: "0"}, nil)
	registerCacheWarm(srv, c)

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

// TestWarmPackagesReachesTheWarmRoute checks the request an agent's arguments
// turn into, `source` and `platforms` included: both are optional, so a struct
// tag typo on either would be invisible in every other test.
func TestWarmPackagesReachesTheWarmRoute(t *testing.T) {
	var gotPath string
	var gotBody map[string]any
	sess := warmToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &gotBody)
		w.WriteHeader(http.StatusAccepted)
		_, _ = w.Write([]byte(`{"data":{"id":"warm-abc","attributes":{"status":"queued","total":1}}}`))
	})

	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "terrapod_cache_warm_packages",
		Arguments: map[string]any{
			"packages": []any{map[string]any{
				"ecosystem": "galaxy",
				"name":      "community.general",
				"version":   "9.2.0",
				"source":    "hub",
			}},
		},
	})
	if err != nil {
		t.Fatalf("CallTool: %v", err)
	}
	if res.IsError {
		t.Fatalf("tool errored: %+v", res.Content)
	}
	if gotPath != "/api/v1/admin/package-cache/warm" {
		t.Errorf("path = %q", gotPath)
	}
	packages, _ := gotBody["packages"].([]any)
	if len(packages) != 1 {
		t.Fatalf("packages = %v", gotBody["packages"])
	}
	spec, _ := packages[0].(map[string]any)
	if spec["ecosystem"] != "galaxy" || spec["version"] != "9.2.0" || spec["source"] != "hub" {
		t.Errorf("spec lost a field: %v", spec)
	}
}

func TestWarmImagesReachesTheOCIRoute(t *testing.T) {
	var gotPath string
	sess := warmToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		w.WriteHeader(http.StatusAccepted)
		_, _ = w.Write([]byte(`{"data":{"id":"warm-img","attributes":{"status":"queued","total":1}}}`))
	})

	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name:      "terrapod_cache_warm_images",
		Arguments: map[string]any{"images": []any{"quay.io/a/b:1"}},
	})
	if err != nil {
		t.Fatalf("CallTool: %v", err)
	}
	if res.IsError {
		t.Fatalf("tool errored: %+v", res.Content)
	}
	if gotPath != "/api/v1/admin/oci/warm" {
		t.Errorf("path = %q", gotPath)
	}
}

// TestWarmStatusSurfacesAlreadyCached is the one outcome field an agent would
// otherwise have no way to reason about: warming is idempotent, so a re-run is
// the normal way to close the last gaps, and without this a converged run looks
// exactly like a first one.
func TestWarmStatusSurfacesAlreadyCached(t *testing.T) {
	sess := warmToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasPrefix(r.URL.Path, "/api/v1/admin/warm-jobs/") {
			http.Error(w, "unhandled", http.StatusNotFound)
			return
		}
		_, _ = w.Write([]byte(`{"data":{"id":"warm-abc","attributes":{
		  "status":"finished","total":1,"completed":1,"succeeded":1,"failed":0,
		  "submitted-at":"2026-01-01T00:00:00Z","outcomes":[
		    {"ecosystem":"pypi","ref":"requests@2.32.3","ok":true,"detail":"",
		     "files":4,"fetched":0,"source":"https://hub.example/pypi"}]}}}`))
	})

	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name:      "terrapod_cache_warm_status",
		Arguments: map[string]any{"job_id": "warm-abc"},
	})
	if err != nil {
		t.Fatalf("CallTool: %v", err)
	}
	var out cacheWarmStatusOut
	if err := json.Unmarshal(mustJSON(t, res.StructuredContent), &out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if !out.Finished {
		t.Error("a finished job must say so, or an agent polls forever")
	}
	if len(out.Outcomes) != 1 {
		t.Fatalf("outcomes = %d", len(out.Outcomes))
	}
	o := out.Outcomes[0]
	if o.Files != 4 || o.Fetched != 0 {
		t.Errorf("already-cached outcome lost its counts: %+v", o)
	}
	if o.Source != "https://hub.example/pypi" {
		t.Errorf("source = %q", o.Source)
	}
}

// TestWarmSourcesSurfacesSealedAndLatest pins the two fields that decide
// whether a submission is worth making at all.
func TestWarmSourcesSurfacesSealedAndLatest(t *testing.T) {
	sess := warmToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/admin/package-cache/sources" {
			http.Error(w, "unhandled", http.StatusNotFound)
			return
		}
		_, _ = w.Write([]byte(`{"data":[
		  {"id":"pulumi","attributes":{"ecosystem":"pulumi","enabled":true,
		   "upstream":"https://get.pulumi.com","mirrors":{},"resolves-latest":false}}
		],"meta":{"max-items":500,"sealed":true}}`))
	})

	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "terrapod_cache_warm_sources", Arguments: map[string]any{},
	})
	if err != nil {
		t.Fatalf("CallTool: %v", err)
	}
	var out cacheWarmSourcesOut
	if err := json.Unmarshal(mustJSON(t, res.StructuredContent), &out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if !out.Sealed {
		t.Error("a sealed node must say so: every warm there can only fail")
	}
	if out.MaxItems != 500 {
		t.Errorf("max_items = %d", out.MaxItems)
	}
	if len(out.Ecosystems) != 1 || out.Ecosystems[0].ResolvesLatest {
		t.Errorf("ecosystems = %+v", out.Ecosystems)
	}
}

// TestWarmAnnotations: the submissions are mutating rather than destructive,
// and the two reads are read-only. Risk is carried by the annotation, which is
// what a host keys its confirmation off -- so this reads it from the live
// server rather than from a list here.
func TestWarmAnnotations(t *testing.T) {
	sess := warmToolCaller(t, func(w http.ResponseWriter, r *http.Request) {})
	tools, err := sess.ListTools(context.Background(), nil)
	if err != nil {
		t.Fatalf("ListTools: %v", err)
	}

	want := map[string]string{
		"terrapod_cache_warm_sources":  "read",
		"terrapod_cache_warm_status":   "read",
		"terrapod_cache_warm_packages": "mutating",
		"terrapod_cache_warm_images":   "mutating",
	}
	seen := map[string]bool{}
	for _, tool := range tools.Tools {
		kind, ok := want[tool.Name]
		if !ok {
			t.Errorf("unexpected tool in this group: %s", tool.Name)
			continue
		}
		seen[tool.Name] = true
		a := tool.Annotations
		if a == nil {
			t.Errorf("%s has no annotations", tool.Name)
			continue
		}
		switch kind {
		case "read":
			if !a.ReadOnlyHint {
				t.Errorf("%s should be read-only", tool.Name)
			}
		case "mutating":
			if a.ReadOnlyHint {
				t.Errorf("%s writes, so it cannot be read-only", tool.Name)
			}
			// Warming ADDS: nothing is removed and nothing a run depends on is
			// changed, so a host should not have to interrupt for it the way it
			// does for an eviction.
			if a.DestructiveHint == nil || *a.DestructiveHint {
				t.Errorf("%s adds to a cache; it is not destructive", tool.Name)
			}
		}
	}
	for name := range want {
		if !seen[name] {
			t.Errorf("%s was not registered", name)
		}
	}
}

// TestWarmDescriptionsCarryTheAsyncContract is the gate the catalogue cannot
// be: a tool-level description is frozen by nothing, and these three claims are
// what stop an agent reporting success off a 202 or recommending a retry on a
// node that is sealed by design.
func TestWarmDescriptionsCarryTheAsyncContract(t *testing.T) {
	sess := warmToolCaller(t, func(w http.ResponseWriter, r *http.Request) {})
	tools, err := sess.ListTools(context.Background(), nil)
	if err != nil {
		t.Fatalf("ListTools: %v", err)
	}
	byName := map[string]string{}
	for _, tool := range tools.Tools {
		byName[tool.Name] = tool.Description
	}

	for _, name := range []string{"terrapod_cache_warm_packages", "terrapod_cache_warm_images"} {
		d := byName[name]
		if !strings.Contains(d, "JOB ID, NOT A RESULT") {
			t.Errorf("%s must say the submission is accepted rather than done", name)
		}
		if !strings.Contains(d, "terrapod_cache_warm_status") {
			t.Errorf("%s must point at the tool that completes the shape", name)
		}
		if !strings.Contains(d, "terrapod_cache_warm_sources") {
			t.Errorf("%s must point at the tool that says whether it can work", name)
		}
	}
	// The explicit-version requirement is the issue's first condition, and an
	// agent that does not know it defaults to the unreproducible form.
	if !strings.Contains(byName["terrapod_cache_warm_packages"], "EXPLICIT VERSIONS") {
		t.Error("the package warm must ask for explicit versions")
	}
	if !strings.Contains(byName["terrapod_cache_warm_sources"], "sealed") {
		t.Error("the sources tool must explain what sealed means")
	}
	// `source` is a label and not a URL, which an agent will otherwise guess
	// wrong in the one direction that matters.
	if !strings.Contains(byName["terrapod_cache_warm_sources"], "never a URL") {
		t.Error("the sources tool must say source is a label")
	}
}
