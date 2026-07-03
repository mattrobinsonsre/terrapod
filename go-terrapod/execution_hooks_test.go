package terrapod

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newHookFixture(t *testing.T) (*Client, *[]byte) {
	t.Helper()
	var lastBody []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Body != nil {
			b, _ := io.ReadAll(r.Body)
			lastBody = b
			_ = r.Body.Close()
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/terrapod/v1/execution-hooks":
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"hook-aaa","type":"execution-hooks","attributes":{
			  "name":"hosts","description":"add hosts entry","hook-point":"pre_init","script":"echo hi",
			  "enabled":true,"priority":5,"workspace-count":0
			}}}`))
		case r.Method == http.MethodGet && r.URL.Path == "/api/terrapod/v1/execution-hooks":
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"hook-aaa","type":"execution-hooks","attributes":{"name":"hosts","hook-point":"pre_init"}},
			  {"id":"hook-bbb","type":"execution-hooks","attributes":{"name":"notify","hook-point":"post_apply"}}
			]}`))
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/terrapod/v1/execution-hooks/"):
			_, _ = w.Write([]byte(`{"data":{"id":"hook-aaa","type":"execution-hooks","attributes":{"name":"hosts","hook-point":"pre_init"},
			  "relationships":{"workspaces":{"data":[{"id":"ws-app","type":"workspaces"},{"id":"ws-api","type":"workspaces"}]}}
			}}`))
		case r.Method == http.MethodPatch:
			_, _ = w.Write([]byte(`{"data":{"id":"hook-aaa","type":"execution-hooks","attributes":{"name":"hosts","hook-point":"post_apply","priority":9,"enabled":false}}}`))
		case r.Method == http.MethodPost && strings.Contains(r.URL.Path, "/relationships/workspaces"):
			w.WriteHeader(http.StatusNoContent)
		case r.Method == http.MethodDelete && strings.Contains(r.URL.Path, "/relationships/workspaces"):
			w.WriteHeader(http.StatusNoContent)
		case r.Method == http.MethodDelete:
			w.WriteHeader(http.StatusNoContent)
		default:
			http.Error(w, "unhandled", http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c, &lastBody
}

func TestCreateExecutionHook(t *testing.T) {
	c, lastBody := newHookFixture(t)
	h, err := c.CreateExecutionHook(t.Context(), CreateExecutionHookRequest{
		Name:      "hosts",
		HookPoint: "pre_init",
		Script:    "echo hi",
		Enabled:   true,
		Priority:  5,
	})
	if err != nil {
		t.Fatal(err)
	}
	if h.ID != "hook-aaa" || h.HookPoint != "pre_init" || h.Priority != 5 || !h.Enabled {
		t.Errorf("hook: %+v", h)
	}
	// Create must always send hook-point + enabled + priority.
	var req struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	_ = json.Unmarshal(*lastBody, &req)
	if req.Data.Attributes["hook-point"] != "pre_init" {
		t.Errorf("hook-point not sent: %+v", req.Data.Attributes)
	}
}

func TestListExecutionHooks(t *testing.T) {
	c, _ := newHookFixture(t)
	list, err := c.ListExecutionHooks(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(list) != 2 {
		t.Errorf("list: %+v", list)
	}
}

func TestUpdateExecutionHook_PointerSemantics(t *testing.T) {
	c, lastBody := newHookFixture(t)
	point := "post_apply"
	_, err := c.UpdateExecutionHook(t.Context(), "hook-aaa", UpdateExecutionHookRequest{
		HookPoint: &point,
	})
	if err != nil {
		t.Fatal(err)
	}
	var req struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	_ = json.Unmarshal(*lastBody, &req)
	if req.Data.Attributes["hook-point"] != "post_apply" {
		t.Fatalf("hook-point not patched: %+v", req.Data.Attributes)
	}
	if _, has := req.Data.Attributes["name"]; has {
		t.Errorf("name leaked: %+v", req.Data.Attributes)
	}
	if _, has := req.Data.Attributes["enabled"]; has {
		t.Errorf("enabled leaked: %+v", req.Data.Attributes)
	}
}

func TestAssignWorkspaceToExecutionHook(t *testing.T) {
	c, lastBody := newHookFixture(t)
	if err := c.AssignWorkspaceToExecutionHook(t.Context(), "hook-aaa", "ws-app"); err != nil {
		t.Fatal(err)
	}
	var req struct {
		Data []map[string]any `json:"data"`
	}
	_ = json.Unmarshal(*lastBody, &req)
	if len(req.Data) != 1 || req.Data[0]["id"] != "ws-app" || req.Data[0]["type"] != "workspaces" {
		t.Errorf("body: %+v", req)
	}
}

func TestIsWorkspaceAssignedToExecutionHook(t *testing.T) {
	c, _ := newHookFixture(t)
	yes, err := c.IsWorkspaceAssignedToExecutionHook(t.Context(), "hook-aaa", "ws-app")
	if err != nil || !yes {
		t.Errorf("expected assigned, got %v / %v", yes, err)
	}
	no, err := c.IsWorkspaceAssignedToExecutionHook(t.Context(), "hook-aaa", "ws-other")
	if err != nil || no {
		t.Errorf("expected not assigned: %v / %v", no, err)
	}
}

func TestUnassignWorkspaceFromExecutionHook(t *testing.T) {
	c, _ := newHookFixture(t)
	if err := c.UnassignWorkspaceFromExecutionHook(t.Context(), "hook-aaa", "ws-app"); err != nil {
		t.Error(err)
	}
}

func TestDeleteExecutionHook(t *testing.T) {
	c, _ := newHookFixture(t)
	if err := c.DeleteExecutionHook(t.Context(), "hook-aaa"); err != nil {
		t.Error(err)
	}
}
