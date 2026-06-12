package terrapod

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newRoleFixture(t *testing.T) (*Client, *[]byte) {
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
		case r.Method == http.MethodPost && r.URL.Path == "/api/terrapod/v1/roles":
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"name":"sre","type":"roles","attributes":{
			  "description":"SRE team",
			  "workspace-permission":"admin","pool-permission":"admin","registry-permission":"write",
			  "allow-labels":{"team":"sre"},"allow-names":["prod-*"],
			  "deny-labels":{},"deny-names":[],
			  "built-in":false
			}}}`))
		case r.Method == http.MethodGet && r.URL.Path == "/api/terrapod/v1/roles":
			_, _ = w.Write([]byte(`{"data":[
			  {"name":"admin","type":"roles","attributes":{"workspace-permission":"admin","built-in":true}},
			  {"name":"sre","type":"roles","attributes":{"workspace-permission":"admin","built-in":false}}
			]}`))
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/terrapod/v1/roles/"):
			_, _ = w.Write([]byte(`{"data":{"name":"sre","type":"roles","attributes":{
			  "workspace-permission":"admin","pool-permission":"admin",
			  "allow-labels":{"team":"sre"},"built-in":false
			}}}`))
		case r.Method == http.MethodPatch:
			_, _ = w.Write([]byte(`{"data":{"name":"sre","type":"roles","attributes":{
			  "workspace-permission":"admin","pool-permission":"admin",
			  "description":"updated"
			}}}`))
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

func TestCreateRole_FullShape(t *testing.T) {
	c, lastBody := newRoleFixture(t)
	r, err := c.CreateRole(t.Context(), CreateRoleRequest{
		Name:                "sre",
		Description:         "SRE team",
		WorkspacePermission: "admin",
		PoolPermission:      "admin",
		RegistryPermission:  "write",
		AllowLabels:         map[string]string{"team": "sre"},
		AllowNames:          []string{"prod-*"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if r.Name != "sre" || r.AllowLabels["team"] != "sre" || r.WorkspacePermission != "admin" {
		t.Errorf("role: %+v", r)
	}
	if r.RegistryPermission != "write" {
		t.Errorf("registry-permission not parsed: %+v", r)
	}
	// Body shape — "name" at data level, attributes contain the rest.
	var req struct {
		Data struct {
			Name       string         `json:"name"`
			Type       string         `json:"type"`
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	_ = json.Unmarshal(*lastBody, &req)
	if req.Data.Name != "sre" || req.Data.Type != "roles" {
		t.Errorf("envelope wrong: %+v", req.Data)
	}
	if req.Data.Attributes["workspace-permission"] != "admin" {
		t.Errorf("workspace-permission missing: %+v", req.Data.Attributes)
	}
	if req.Data.Attributes["registry-permission"] != "write" {
		t.Errorf("registry-permission not sent: %+v", req.Data.Attributes)
	}
}

func TestCreateRole_AlwaysSendsEmptyAllowDeny(t *testing.T) {
	// Allow/deny fields should always be present in the create body —
	// the server uses absence vs empty differently and we want
	// "no allow rules" rather than "leave default" on create.
	c, lastBody := newRoleFixture(t)
	_, err := c.CreateRole(t.Context(), CreateRoleRequest{
		Name:                "minimal",
		WorkspacePermission: "read",
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
	for _, key := range []string{"allow-labels", "allow-names", "deny-labels", "deny-names"} {
		if _, has := req.Data.Attributes[key]; !has {
			t.Errorf("%s missing from create body: %+v", key, req.Data.Attributes)
		}
	}
}

func TestGetRole(t *testing.T) {
	c, _ := newRoleFixture(t)
	r, err := c.GetRole(t.Context(), "sre")
	if err != nil {
		t.Fatal(err)
	}
	if r.AllowLabels["team"] != "sre" {
		t.Errorf("role: %+v", r)
	}
}

func TestListRoles_BuiltInFlag(t *testing.T) {
	c, _ := newRoleFixture(t)
	roles, err := c.ListRoles(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(roles) != 2 {
		t.Fatalf("got %d roles", len(roles))
	}
	if !roles[0].BuiltIn {
		t.Errorf("admin should be built-in: %+v", roles[0])
	}
}

func TestUpdateRole_LeaveAllowAlone(t *testing.T) {
	c, lastBody := newRoleFixture(t)
	desc := "updated"
	_, err := c.UpdateRole(t.Context(), "sre", UpdateRoleRequest{
		Description: &desc,
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
	if _, has := req.Data.Attributes["allow-labels"]; has {
		t.Errorf("allow-labels leaked into PATCH: %+v", req.Data.Attributes)
	}
	if req.Data.Attributes["description"] != "updated" {
		t.Errorf("description: %+v", req.Data.Attributes)
	}
}

func TestUpdateRole_ClearAllow(t *testing.T) {
	c, lastBody := newRoleFixture(t)
	empty := map[string]string{}
	_, err := c.UpdateRole(t.Context(), "sre", UpdateRoleRequest{
		AllowLabels: &empty,
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
	if labels, has := req.Data.Attributes["allow-labels"]; !has {
		t.Error("allow-labels missing from PATCH body")
	} else if m, ok := labels.(map[string]any); !ok || len(m) != 0 {
		t.Errorf("allow-labels should be empty: %v", labels)
	}
}

func TestDeleteRole(t *testing.T) {
	c, _ := newRoleFixture(t)
	if err := c.DeleteRole(t.Context(), "sre"); err != nil {
		t.Error(err)
	}
}
