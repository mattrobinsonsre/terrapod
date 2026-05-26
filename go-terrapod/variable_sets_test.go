package terrapod

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newVarsetFixture(t *testing.T) (*Client, *[]byte) {
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
		case r.Method == http.MethodPost && r.URL.Path == "/api/v2/organizations/default/varsets":
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"varset-aaa","type":"varsets","attributes":{
			  "name":"shared","description":"shared vars","global":true,"priority":false,
			  "var-count":3,"workspace-count":0
			}}}`))
		case r.Method == http.MethodGet && r.URL.Path == "/api/v2/organizations/default/varsets":
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"varset-aaa","type":"varsets","attributes":{"name":"shared","global":true}},
			  {"id":"varset-bbb","type":"varsets","attributes":{"name":"team","global":false}}
			]}`))
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/v2/varsets/"):
			_, _ = w.Write([]byte(`{"data":{"id":"varset-aaa","type":"varsets","attributes":{"name":"shared","global":true},
			  "relationships":{"workspaces":{"data":[{"id":"ws-app","type":"workspaces"},{"id":"ws-api","type":"workspaces"}]}}
			}}`))
		case r.Method == http.MethodPatch:
			_, _ = w.Write([]byte(`{"data":{"id":"varset-aaa","type":"varsets","attributes":{"name":"renamed","global":false,"priority":true}}}`))
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

func TestCreateVariableSet(t *testing.T) {
	c, _ := newVarsetFixture(t)
	v, err := c.CreateVariableSet(t.Context(), CreateVariableSetRequest{
		Name:        "shared",
		Description: "shared vars",
		Global:      true,
	})
	if err != nil {
		t.Fatal(err)
	}
	if v.ID != "varset-aaa" || !v.Global {
		t.Errorf("varset: %+v", v)
	}
}

func TestListVariableSets(t *testing.T) {
	c, _ := newVarsetFixture(t)
	list, err := c.ListVariableSets(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(list) != 2 {
		t.Errorf("list: %+v", list)
	}
}

func TestUpdateVariableSet_PointerSemantics(t *testing.T) {
	c, lastBody := newVarsetFixture(t)
	off := false
	_, err := c.UpdateVariableSet(t.Context(), "varset-aaa", UpdateVariableSetRequest{
		Global: &off,
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
	v, has := req.Data.Attributes["global"]
	if !has {
		t.Fatal("global missing")
	}
	if v.(bool) {
		t.Errorf("global should be false: %v", v)
	}
	if _, has := req.Data.Attributes["name"]; has {
		t.Errorf("name leaked: %+v", req.Data.Attributes)
	}
}

func TestAssignWorkspaceToVarset(t *testing.T) {
	c, lastBody := newVarsetFixture(t)
	if err := c.AssignWorkspaceToVarset(t.Context(), "varset-aaa", "ws-app"); err != nil {
		t.Fatal(err)
	}
	// Body shape: {"data":[{"id":"ws-app","type":"workspaces"}]}
	var req struct {
		Data []map[string]any `json:"data"`
	}
	_ = json.Unmarshal(*lastBody, &req)
	if len(req.Data) != 1 || req.Data[0]["id"] != "ws-app" {
		t.Errorf("body: %+v", req)
	}
}

func TestIsWorkspaceAssignedToVarset(t *testing.T) {
	c, _ := newVarsetFixture(t)
	yes, err := c.IsWorkspaceAssignedToVarset(t.Context(), "varset-aaa", "ws-app")
	if err != nil || !yes {
		t.Errorf("expected assigned, got %v / %v", yes, err)
	}
	no, err := c.IsWorkspaceAssignedToVarset(t.Context(), "varset-aaa", "ws-other")
	if err != nil || no {
		t.Errorf("expected not assigned: %v / %v", no, err)
	}
}

func TestUnassignWorkspaceFromVarset(t *testing.T) {
	c, _ := newVarsetFixture(t)
	if err := c.UnassignWorkspaceFromVarset(t.Context(), "varset-aaa", "ws-app"); err != nil {
		t.Error(err)
	}
}

func TestDeleteVariableSet(t *testing.T) {
	c, _ := newVarsetFixture(t)
	if err := c.DeleteVariableSet(t.Context(), "varset-aaa"); err != nil {
		t.Error(err)
	}
}
