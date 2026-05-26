package terrapod

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// workspaceFixtureServer spins up an httptest server that handles
// every workspace endpoint. Per-test scenarios layer in the expected
// behaviour via the per-method handlers; defaults return a minimal
// shape that exercises the happy path.
//
// Bodies are minimal-but-realistic JSON:API; the SDK's parsers don't
// look at fields the tests don't set. Keeping fixtures small leaves
// the tests readable.
type workspaceFixtureServer struct {
	t              *testing.T
	server         *httptest.Server
	createHandler  http.HandlerFunc
	readHandler    http.HandlerFunc
	updateHandler  http.HandlerFunc
	deleteHandler  http.HandlerFunc
	listHandler    http.HandlerFunc
	byNameHandler  http.HandlerFunc
	lastBody       []byte // captures the last request's body for inspection
}

func newWorkspaceFixtureServer(t *testing.T) *workspaceFixtureServer {
	t.Helper()
	f := &workspaceFixtureServer{t: t}
	f.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Body != nil {
			f.lastBody, _ = io.ReadAll(r.Body)
			_ = r.Body.Close()
			r.Body = io.NopCloser(strings.NewReader(string(f.lastBody)))
		}
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/v2/organizations/default/workspaces":
			f.createHandler(w, r)
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/v2/organizations/default/workspaces/"):
			f.byNameHandler(w, r)
		case r.Method == http.MethodGet && r.URL.Path == "/api/v2/organizations/default/workspaces":
			f.listHandler(w, r)
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/v2/workspaces/"):
			f.readHandler(w, r)
		case r.Method == http.MethodPatch && strings.HasPrefix(r.URL.Path, "/api/v2/workspaces/"):
			f.updateHandler(w, r)
		case r.Method == http.MethodDelete && strings.HasPrefix(r.URL.Path, "/api/terrapod/v1/workspaces/"):
			f.deleteHandler(w, r)
		default:
			t.Logf("unhandled request: %s %s", r.Method, r.URL.Path)
			http.Error(w, "unhandled", http.StatusNotFound)
		}
	}))
	t.Cleanup(f.server.Close)
	return f
}

func (f *workspaceFixtureServer) client() *Client {
	c, err := NewClient(Options{BaseURL: f.server.URL, Token: "t"})
	if err != nil {
		f.t.Fatal(err)
	}
	return c
}

// minimalWorkspaceBody returns a JSON:API single-resource doc with
// the named workspace. Caller passes the id + name + optional extras.
func minimalWorkspaceBody(id, name string, extras map[string]any) string {
	attrs := map[string]any{"name": name}
	for k, v := range extras {
		attrs[k] = v
	}
	doc := map[string]any{
		"data": map[string]any{
			"id":         id,
			"type":       "workspaces",
			"attributes": attrs,
		},
	}
	b, _ := json.Marshal(doc)
	return string(b)
}

func TestCreateWorkspace_Happy(t *testing.T) {
	f := newWorkspaceFixtureServer(t)
	f.createHandler = func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(minimalWorkspaceBody("ws-aaa", "api-prod", map[string]any{
			"execution-mode": "agent",
			"auto-apply":     true,
		})))
	}

	c := f.client()
	autoApply := true
	ws, err := c.CreateWorkspace(t.Context(), CreateWorkspaceRequest{
		Name:          "api-prod",
		ExecutionMode: "agent",
		AutoApply:     &autoApply,
		Labels:        map[string]string{"env": "prod"},
	})
	if err != nil {
		t.Fatalf("CreateWorkspace: %v", err)
	}
	if ws.ID != "ws-aaa" || ws.Name != "api-prod" || ws.ExecutionMode != "agent" || !ws.AutoApply {
		t.Errorf("workspace: %+v", ws)
	}

	// Verify the request body carried the expected attributes.
	var req struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(f.lastBody, &req); err != nil {
		t.Fatalf("request body: %v", err)
	}
	if req.Data.Attributes["name"] != "api-prod" || req.Data.Attributes["execution-mode"] != "agent" {
		t.Errorf("request attrs: %+v", req.Data.Attributes)
	}
	// auto-apply was set via pointer — its presence and value should round-trip.
	if v, ok := req.Data.Attributes["auto-apply"].(bool); !ok || !v {
		t.Errorf("auto-apply not set in request: %+v", req.Data.Attributes)
	}
}

func TestCreateWorkspace_WithVCSConnection_BuildsRelationship(t *testing.T) {
	f := newWorkspaceFixtureServer(t)
	f.createHandler = func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(minimalWorkspaceBody("ws-aaa", "api-prod", nil)))
	}
	c := f.client()
	_, err := c.CreateWorkspace(t.Context(), CreateWorkspaceRequest{
		Name:            "api-prod",
		VCSConnectionID: "vcs-aaa",
	})
	if err != nil {
		t.Fatal(err)
	}
	// VCSConnectionID goes into relationships, not attributes.
	var req struct {
		Data struct {
			Attributes    map[string]any `json:"attributes"`
			Relationships map[string]any `json:"relationships"`
		} `json:"data"`
	}
	_ = json.Unmarshal(f.lastBody, &req)
	if _, has := req.Data.Attributes["vcs-connection-id"]; has {
		t.Error("vcs-connection-id should not be in attributes")
	}
	conn, ok := req.Data.Relationships["vcs-connection"].(map[string]any)
	if !ok {
		t.Fatalf("relationships.vcs-connection missing: %+v", req.Data.Relationships)
	}
	data, ok := conn["data"].(map[string]any)
	if !ok || data["id"] != "vcs-aaa" || data["type"] != "vcs-connections" {
		t.Errorf("vcs-connection relationship: %+v", conn)
	}
}

func TestCreateWorkspace_Conflict409(t *testing.T) {
	f := newWorkspaceFixtureServer(t)
	f.createHandler = func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"errors":[{"status":"409","detail":"name already taken"}]}`))
	}
	c := f.client()
	_, err := c.CreateWorkspace(t.Context(), CreateWorkspaceRequest{Name: "api-prod"})
	if !IsConflict(err) {
		t.Errorf("expected ConflictError, got: %v", err)
	}
}

func TestGetWorkspace_Happy(t *testing.T) {
	f := newWorkspaceFixtureServer(t)
	f.readHandler = func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasSuffix(r.URL.Path, "/ws-aaa") {
			t.Errorf("wrong path: %s", r.URL.Path)
		}
		_, _ = w.Write([]byte(minimalWorkspaceBody("ws-aaa", "api-prod", nil)))
	}
	c := f.client()
	ws, err := c.GetWorkspace(t.Context(), "ws-aaa")
	if err != nil {
		t.Fatalf("GetWorkspace: %v", err)
	}
	if ws.ID != "ws-aaa" || ws.Name != "api-prod" {
		t.Errorf("workspace: %+v", ws)
	}
}

func TestGetWorkspace_NotFound(t *testing.T) {
	f := newWorkspaceFixtureServer(t)
	f.readHandler = func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"errors":[{"status":"404"}]}`))
	}
	c := f.client()
	_, err := c.GetWorkspace(t.Context(), "ws-missing")
	if !IsNotFound(err) {
		t.Errorf("expected NotFoundError, got: %v", err)
	}
}

func TestGetWorkspaceByName(t *testing.T) {
	f := newWorkspaceFixtureServer(t)
	f.byNameHandler = func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasSuffix(r.URL.Path, "/api-prod") {
			t.Errorf("wrong path: %s", r.URL.Path)
		}
		_, _ = w.Write([]byte(minimalWorkspaceBody("ws-aaa", "api-prod", nil)))
	}
	c := f.client()
	ws, err := c.GetWorkspaceByName(t.Context(), "api-prod")
	if err != nil {
		t.Fatalf("GetWorkspaceByName: %v", err)
	}
	if ws.ID != "ws-aaa" {
		t.Errorf("workspace: %+v", ws)
	}
}

func TestUpdateWorkspace_PartialUpdate(t *testing.T) {
	f := newWorkspaceFixtureServer(t)
	f.updateHandler = func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(minimalWorkspaceBody("ws-aaa", "api-prod", map[string]any{
			"terraform-version": "1.12.0",
		})))
	}
	c := f.client()
	ws, err := c.UpdateWorkspace(t.Context(), "ws-aaa", UpdateWorkspaceRequest{
		TerraformVersion: "1.12.0",
	})
	if err != nil {
		t.Fatalf("UpdateWorkspace: %v", err)
	}
	if ws.TerraformVersion != "1.12.0" {
		t.Errorf("terraform version: %q", ws.TerraformVersion)
	}
	// Update body shouldn't have set any other attribute the operator
	// didn't pass — pointer fields stay absent when nil.
	var req struct {
		Data struct {
			ID         string         `json:"id"`
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	_ = json.Unmarshal(f.lastBody, &req)
	if req.Data.ID != "ws-aaa" {
		t.Errorf("id missing from body: %+v", req.Data)
	}
	if _, has := req.Data.Attributes["auto-apply"]; has {
		t.Errorf("auto-apply leaked into request when not set: %+v", req.Data.Attributes)
	}
}

func TestDeleteWorkspace_UsesTerrapodNativePath(t *testing.T) {
	// Workspace delete lives on /api/terrapod/v1/, not /api/v2/.
	// This is critical — /api/v2/workspaces/{id} returns 405 (see
	// provider #353). Regression-protect the path here.
	f := newWorkspaceFixtureServer(t)
	var calledPath string
	f.deleteHandler = func(w http.ResponseWriter, r *http.Request) {
		calledPath = r.URL.Path
		w.WriteHeader(http.StatusNoContent)
	}
	c := f.client()
	if err := c.DeleteWorkspace(t.Context(), "ws-aaa"); err != nil {
		t.Fatalf("DeleteWorkspace: %v", err)
	}
	if calledPath != "/api/terrapod/v1/workspaces/ws-aaa" {
		t.Errorf("wrong delete path: %q (must be Terrapod-native, NOT /api/v2/)", calledPath)
	}
}

func TestDeleteWorkspace_NotFoundReturnsError(t *testing.T) {
	// Idempotent-delete UX is the caller's choice — the SDK returns
	// the typed error and lets the caller decide whether to swallow
	// (terrapod-migrate retries) or surface (provider Update flow).
	f := newWorkspaceFixtureServer(t)
	f.deleteHandler = func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"errors":[{"status":"404"}]}`))
	}
	c := f.client()
	err := c.DeleteWorkspace(t.Context(), "ws-aaa")
	if !IsNotFound(err) {
		t.Errorf("expected NotFoundError, got: %v", err)
	}
}

func TestListWorkspaces_PaginationMeta(t *testing.T) {
	f := newWorkspaceFixtureServer(t)
	f.listHandler = func(w http.ResponseWriter, r *http.Request) {
		if got := r.URL.Query().Get("page[number]"); got != "2" {
			t.Errorf("page number = %q, want %q", got, "2")
		}
		if got := r.URL.Query().Get("page[size]"); got != "50" {
			t.Errorf("page size = %q, want %q", got, "50")
		}
		_, _ = w.Write([]byte(`{
		  "data": [
		    {"id":"ws-aaa","type":"workspaces","attributes":{"name":"api"}},
		    {"id":"ws-bbb","type":"workspaces","attributes":{"name":"web"}}
		  ],
		  "meta": {"pagination": {"current-page": 2, "total-pages": 5, "total-count": 100}}
		}`))
	}
	c := f.client()
	list, err := c.ListWorkspaces(t.Context(), WorkspaceListOptions{PageNumber: 2, PageSize: 50})
	if err != nil {
		t.Fatalf("ListWorkspaces: %v", err)
	}
	if len(list.Items) != 2 || list.Items[0].ID != "ws-aaa" {
		t.Errorf("items: %+v", list.Items)
	}
	if list.CurrentPage != 2 || list.TotalPages != 5 || list.TotalCount != 100 {
		t.Errorf("pagination: %+v", list)
	}
}

func TestListWorkspaces_SearchFilter(t *testing.T) {
	f := newWorkspaceFixtureServer(t)
	f.listHandler = func(w http.ResponseWriter, r *http.Request) {
		if got := r.URL.Query().Get("search[name]"); got != "prod" {
			t.Errorf("search filter = %q", got)
		}
		_, _ = w.Write([]byte(`{"data": [], "meta": {"pagination": {}}}`))
	}
	c := f.client()
	_, err := c.ListWorkspaces(t.Context(), WorkspaceListOptions{Search: "prod"})
	if err != nil {
		t.Fatal(err)
	}
}

func TestWorkspaceFromResource_DriftFields(t *testing.T) {
	body := `{"data": {
	  "id": "ws-aaa",
	  "type": "workspaces",
	  "attributes": {
	    "name": "api",
	    "drift-detection-enabled": true,
	    "drift-detection-interval-seconds": 300,
	    "drift-status": "ok",
	    "drift-last-checked-at": "2026-01-02T03:04:05Z"
	  }
	}}`
	ws, err := parseWorkspace([]byte(body))
	if err != nil {
		t.Fatal(err)
	}
	if !ws.DriftDetectionEnabled {
		t.Error("DriftDetectionEnabled")
	}
	if ws.DriftDetectionIntervalSeconds == nil || *ws.DriftDetectionIntervalSeconds != 300 {
		t.Errorf("DriftDetectionIntervalSeconds: %v", ws.DriftDetectionIntervalSeconds)
	}
	if ws.DriftStatus != "ok" || ws.DriftLastCheckedAt != "2026-01-02T03:04:05Z" {
		t.Errorf("drift fields: %+v", ws)
	}
}

func TestWorkspaceFromResource_VCSConnectionRelationship(t *testing.T) {
	body := `{"data": {
	  "id": "ws-aaa",
	  "type": "workspaces",
	  "attributes": {"name": "api"},
	  "relationships": {
	    "vcs-connection": {"data": {"id": "vcs-xyz", "type": "vcs-connections"}}
	  }
	}}`
	ws, err := parseWorkspace([]byte(body))
	if err != nil {
		t.Fatal(err)
	}
	if ws.VCSConnectionID != "vcs-xyz" {
		t.Errorf("VCSConnectionID = %q", ws.VCSConnectionID)
	}
}

func TestWorkspaceCreate_RetryOn5xx(t *testing.T) {
	// Belt-and-braces verification that the workspace-specific path
	// still picks up Client's 5xx retry. The Client tests cover this
	// abstractly; this confirms the path the migration tool actually
	// drives.
	var calls int
	f := newWorkspaceFixtureServer(t)
	f.createHandler = func(w http.ResponseWriter, r *http.Request) {
		calls++
		if calls == 1 {
			w.WriteHeader(http.StatusBadGateway)
			return
		}
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(minimalWorkspaceBody("ws-aaa", "api", nil)))
	}
	c := f.client()
	if _, err := c.CreateWorkspace(context.Background(), CreateWorkspaceRequest{Name: "api"}); err != nil {
		t.Fatalf("retry should recover: %v", err)
	}
	if calls != 2 {
		t.Errorf("expected 2 calls (1 retry), got %d", calls)
	}
}
