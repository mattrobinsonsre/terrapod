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
		AllowLabels:         map[string][]string{"team": {"sre"}},
		AllowNames:          []string{"prod-*"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if r.Name != "sre" || len(r.AllowLabels["team"]) != 1 || r.AllowLabels["team"][0] != "sre" ||
		r.WorkspacePermission != "admin" {
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
	if len(r.AllowLabels["team"]) != 1 || r.AllowLabels["team"][0] != "sre" {
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
	empty := map[string][]string{}
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

func TestRole_CapabilitiesRoundTrip(t *testing.T) {
	var lastBody []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Body != nil {
			b, _ := io.ReadAll(r.Body)
			lastBody = b
			_ = r.Body.Close()
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusCreated)
		// Server echoes the effective caps + a derived "custom" workspace level.
		_, _ = w.Write([]byte(`{"data":{"name":"granular","type":"roles","attributes":{
		  "workspace-permission":"custom","pool-permission":"read","registry-permission":"read",
		  "catalog-permission":"none","capabilities":["run:plan","run:read","var:read","var:write"],
		  "built-in":false
		}}}`))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	r, err := c.CreateRole(t.Context(), CreateRoleRequest{
		Name:         "granular",
		Capabilities: []string{"run:read", "run:plan", "var:read", "var:write"},
	})
	if err != nil {
		t.Fatal(err)
	}
	// Request carries the capabilities list.
	if !strings.Contains(string(lastBody), `"capabilities"`) ||
		!strings.Contains(string(lastBody), `"run:plan"`) {
		t.Errorf("create body missing capabilities: %s", lastBody)
	}
	// Response capabilities are parsed back (regression: roleFromItem must read
	// the capabilities attribute, not drop it).
	if len(r.Capabilities) != 4 {
		t.Fatalf("capabilities not parsed: %+v", r.Capabilities)
	}
	if r.WorkspacePermission != "custom" {
		t.Errorf("derived level should be custom: %q", r.WorkspacePermission)
	}
}

// ── Role reach preview (#1456) ─────────────────────────────────────────

func newReachFixture(t *testing.T) (*Client, *http.Request, *[]byte) {
	t.Helper()
	var gotReq *http.Request
	var gotBody []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotReq = r.Clone(r.Context())
		gotBody, _ = io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(`{"data":{"type":"role-previews","id":"sre","attributes":{
			"granted-count": 47,
			"denied-count": 3,
			"matched-count": 50,
			"denied-truncated": false,
			"workspaces": [{
				"id":"ws-1","name":"prod-api","labels":{"env":"prod"},
				"owner-email":"a@b.c","verdict":"allowed",
				"reason":"allow-label:env=prod",
				"capabilities":["run:apply","run:plan"],
				"notes":["has-owner"]
			}],
			"denied": [{
				"id":"ws-2","name":"prod-locked","verdict":"denied",
				"reason":"deny-label:locked-down=yes","capabilities":[]
			}]
		}}}`))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c, gotReq, &gotBody
}

func TestPreviewRoleReach(t *testing.T) {
	c, _, _ := newReachFixture(t)
	reach, err := c.PreviewRoleReach(t.Context(), "sre", nil)
	if err != nil {
		t.Fatal(err)
	}
	// Counts are fleet-wide, not page-wide — the whole point of the feature.
	if reach.GrantedCount != 47 || reach.DeniedCount != 3 || reach.MatchedCount != 50 {
		t.Errorf("counts: %+v", reach)
	}
	if len(reach.Workspaces) != 1 || reach.Workspaces[0].Name != "prod-api" {
		t.Fatalf("workspaces: %+v", reach.Workspaces)
	}
	// The reason is the thing that makes the answer reviewable rather than
	// merely correct.
	if reach.Workspaces[0].Reason != "allow-label:env=prod" {
		t.Errorf("reason: %q", reach.Workspaces[0].Reason)
	}
	if reach.Workspaces[0].Verdict != RoleReachAllowed {
		t.Errorf("verdict: %q", reach.Workspaces[0].Verdict)
	}
	if len(reach.Workspaces[0].Notes) != 1 || reach.Workspaces[0].Notes[0] != RoleReachNoteHasOwner {
		t.Errorf("notes: %+v", reach.Workspaces[0].Notes)
	}
	// Denied is populated, not silently folded away.
	if len(reach.Denied) != 1 || reach.Denied[0].Reason != "deny-label:locked-down=yes" {
		t.Errorf("denied: %+v", reach.Denied)
	}
}

func TestPreviewRoleReach_Paging(t *testing.T) {
	var path string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path = r.URL.RequestURI()
		_, _ = w.Write([]byte(`{"data":{"attributes":{"granted-count":0,"workspaces":[]}}}`))
	}))
	defer srv.Close()
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.PreviewRoleReach(t.Context(), "sre", &RoleReachOptions{PageSize: 5, PageNumber: 3}); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(path, "page%5Bsize%5D=5") || !strings.Contains(path, "page%5Bnumber%5D=3") {
		t.Errorf("paging not sent: %s", path)
	}
}

func TestPreviewUnsavedRoleReach_SendsTheRuleAndPersistsNothing(t *testing.T) {
	c, _, bodyp := newReachFixture(t)
	reach, err := c.PreviewUnsavedRoleReach(t.Context(), CreateRoleRequest{
		Name:                "draft",
		AllowLabels:         map[string][]string{"env": {"prod"}},
		DenyNames:           []string{"prod-locked"},
		WorkspacePermission: "write",
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if reach.GrantedCount != 47 {
		t.Errorf("reach: %+v", reach)
	}
	// The rule must reach the server, including the name, or the preview is of
	// a different role than the one being authored.
	var sent map[string]any
	if err := json.Unmarshal(*bodyp, &sent); err != nil {
		t.Fatal(err)
	}
	attrs := sent["data"].(map[string]any)["attributes"].(map[string]any)
	if attrs["name"] != "draft" {
		t.Errorf("name not sent: %v", attrs)
	}
	if attrs["workspace-permission"] != "write" {
		t.Errorf("permission not sent: %v", attrs)
	}
	if _, ok := attrs["allow-labels"]; !ok {
		t.Errorf("allow-labels not sent: %v", attrs)
	}
	if _, ok := attrs["deny-names"]; !ok {
		t.Errorf("deny-names not sent: %v", attrs)
	}
}

func TestPreviewRoleReach_BuiltinIsServerRejected(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(422)
		_, _ = w.Write([]byte(`{"detail":"'admin' is a built-in role"}`))
	}))
	defer srv.Close()
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.PreviewRoleReach(t.Context(), "admin", nil); err == nil {
		t.Fatal("expected an error for a built-in role")
	}
}

func TestPreviewRoleReach_AxisBreakdown(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"data":{"attributes":{
			"granted-count": 4, "denied-count": 0, "matched-count": 4,
			"axes": {
				"workspace": {"granted-count":2,"resources":[
					{"id":"ws-1","kind":"workspaces","name":"prod-api","verdict":"allowed","capabilities":["run:apply"]}]},
				"pool":      {"granted-count":1,"resources":[
					{"id":"apool-1","kind":"agent-pools","name":"eu-runners","verdict":"allowed","capabilities":["pool:update"]}]},
				"registry":  {"granted-count":1,"resources":[
					{"id":"m1","kind":"registry-modules","name":"vpc","verdict":"allowed","capabilities":["registry:publish"]}]},
				"catalog":   {"granted-count":0,"resources":[]}
			},
			"workspaces": [{"id":"ws-1","name":"prod-api","verdict":"allowed"}]
		}}}`))
	}))
	defer srv.Close()
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	reach, err := c.PreviewRoleReach(t.Context(), "sre", nil)
	if err != nil {
		t.Fatal(err)
	}
	// The whole point: one rule reaches more than workspaces, and a caller
	// reading only the workspace numbers gets a quarter of the answer.
	if reach.GrantedCount != 4 {
		t.Errorf("total should span every axis, got %d", reach.GrantedCount)
	}
	if reach.Axes["pool"].GrantedCount != 1 || reach.Axes["registry"].GrantedCount != 1 {
		t.Errorf("axes: %+v", reach.Axes)
	}
	// Capabilities are sliced per axis.
	if got := reach.Axes["pool"].Resources[0].Capabilities; len(got) != 1 || got[0] != "pool:update" {
		t.Errorf("pool caps: %v", got)
	}
	if reach.Axes["registry"].Resources[0].Kind != "registry-modules" {
		t.Errorf("kind not carried: %+v", reach.Axes["registry"].Resources[0])
	}
	// The promoted workspace list still works for callers that only want it.
	if len(reach.Workspaces) != 1 || reach.Workspaces[0].Name != "prod-api" {
		t.Errorf("promoted workspaces: %+v", reach.Workspaces)
	}
}

func TestGetResourceAccess(t *testing.T) {
	var path string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path = r.URL.Path
		_, _ = w.Write([]byte(`{"data":{"type":"resource-access","id":"ws-1","attributes":{
			"resource": {"id":"ws-1","kind":"workspaces","name":"prod-api","owner-email":"a@b.c"},
			"axis": "workspace",
			"role-count": 1,
			"roles": [{"role":"sre","verdict":"allowed","reason":"allow-label:env=prod",
			           "capabilities":["run:apply"],"held-by":["alice@example.com"]}],
			"denied-roles": [{"role":"contractors","verdict":"denied","reason":"deny-name"}],
			"platform-paths": ["platform-admin","platform-audit","owner"]
		}}}`))
	}))
	defer srv.Close()
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	acc, err := c.GetResourceAccess(t.Context(), "workspaces", "ws-1")
	if err != nil {
		t.Fatal(err)
	}
	if path != "/api/terrapod/v1/workspaces/ws-1/access" {
		t.Errorf("path: %s", path)
	}
	if len(acc.Roles) != 1 || acc.Roles[0].Role != "sre" {
		t.Fatalf("roles: %+v", acc.Roles)
	}
	// Who holds the role is the actionable half — a role nobody holds reaches
	// nothing in practice.
	if len(acc.Roles[0].HeldBy) != 1 || acc.Roles[0].HeldBy[0] != "alice@example.com" {
		t.Errorf("held-by: %+v", acc.Roles[0].HeldBy)
	}
	if len(acc.DeniedRoles) != 1 || acc.DeniedRoles[0].Reason != "deny-name" {
		t.Errorf("denied: %+v", acc.DeniedRoles)
	}
	// Platform paths must survive decode: omitting them makes a partial answer
	// look complete.
	if len(acc.PlatformPaths) != 3 {
		t.Errorf("platform paths: %+v", acc.PlatformPaths)
	}
}

func TestRole_AllowAll_RoundTrips(t *testing.T) {
	var sent []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sent, _ = io.ReadAll(r.Body)
		_, _ = w.Write([]byte(`{"data":{"name":"platform","attributes":{
			"allow-all": true, "workspace-permission":"write"}}}`))
	}))
	defer srv.Close()
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	r, err := c.CreateRole(t.Context(), CreateRoleRequest{
		Name: "platform", AllowAll: true, WorkspacePermission: "write",
	})
	if err != nil {
		t.Fatal(err)
	}
	var body map[string]any
	if err := json.Unmarshal(sent, &body); err != nil {
		t.Fatal(err)
	}
	attrs := body["data"].(map[string]any)["attributes"].(map[string]any)
	if attrs["allow-all"] != true {
		t.Errorf("allow-all not sent: %v", attrs)
	}
	// An estate-wide grant that decodes as false would make a role reaching
	// everything look like one reaching nothing.
	if !r.AllowAll {
		t.Errorf("allow-all not decoded: %+v", r)
	}
}

func TestRole_AllowAll_UpdateLeaveAlone(t *testing.T) {
	var sent []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sent, _ = io.ReadAll(r.Body)
		_, _ = w.Write([]byte(`{"data":{"name":"x","attributes":{}}}`))
	}))
	defer srv.Close()
	c, _ := NewClient(Options{BaseURL: srv.URL, Token: "t"})

	// nil means leave alone — a rename must not silently revoke an
	// estate-wide grant, nor silently confer one.
	if _, err := c.UpdateRole(t.Context(), "x", UpdateRoleRequest{}); err != nil {
		t.Fatal(err)
	}
	var body map[string]any
	_ = json.Unmarshal(sent, &body)
	if _, present := body["data"].(map[string]any)["attributes"].(map[string]any)["allow-all"]; present {
		t.Error("allow-all must be absent when the pointer is nil")
	}

	off := false
	if _, err := c.UpdateRole(t.Context(), "x", UpdateRoleRequest{AllowAll: &off}); err != nil {
		t.Fatal(err)
	}
	_ = json.Unmarshal(sent, &body)
	attrs := body["data"].(map[string]any)["attributes"].(map[string]any)
	if attrs["allow-all"] != false {
		t.Errorf("explicit false must be sent: %v", attrs)
	}
}
