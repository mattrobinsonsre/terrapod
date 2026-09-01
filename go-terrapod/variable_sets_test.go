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

func TestGetVariableSet(t *testing.T) {
	c, _ := newVarsetFixture(t)
	v, err := c.GetVariableSet(t.Context(), "varset-aaa")
	if err != nil {
		t.Fatal(err)
	}
	if v.ID != "varset-aaa" || v.Name != "shared" || !v.Global {
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

// ── Association views (#1440) ────────────────────────────────────────

func TestListVarsetWorkspacesCarriesTheSource(t *testing.T) {
	// The source is the point of the endpoint, not decoration: it tells a
	// consumer which rows it may offer to unbind and which are derived.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/terrapod/v1/varsets/varset-1/relationships/workspaces" {
			t.Errorf("unexpected path %q", r.URL.Path)
		}
		_, _ = w.Write([]byte(`{"data":[
			{"id":"ws-a","type":"workspaces","attributes":{"name":"alpha","assignment-source":"explicit"}},
			{"id":"ws-b","type":"workspaces","attributes":{"name":"beta","assignment-source":"rule"}}
		]}`))
	}))
	defer srv.Close()

	got, err := mustClient(t, srv).ListVarsetWorkspaces(context.Background(), "varset-1")
	if err != nil {
		t.Fatalf("ListVarsetWorkspaces: %v", err)
	}
	if len(got) != 2 {
		t.Fatalf("want 2 workspaces, got %d", len(got))
	}
	if got[0].AssignmentSource != AssignmentExplicit || got[1].AssignmentSource != AssignmentRuleBased {
		t.Errorf("sources not carried through: %+v", got)
	}
	if got[1].Name != "beta" {
		t.Errorf("name not carried through: %+v", got[1])
	}
}

func TestListWorkspaceVarsetsCarriesTheSource(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/terrapod/v1/workspaces/ws-1/varsets" {
			t.Errorf("unexpected path %q", r.URL.Path)
		}
		_, _ = w.Write([]byte(`{"data":[
			{"id":"varset-x","type":"varsets","attributes":{
				"name":"prod-creds","priority":true,"variable-count":3,"assignment-source":"global"}}
		]}`))
	}))
	defer srv.Close()

	got, err := mustClient(t, srv).ListWorkspaceVarsets(context.Background(), "ws-1")
	if err != nil {
		t.Fatalf("ListWorkspaceVarsets: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("want 1 varset, got %d", len(got))
	}
	if got[0].AssignmentSource != AssignmentGlobal || got[0].VariableCount != 3 || !got[0].Priority {
		t.Errorf("attributes not carried through: %+v", got[0])
	}
}

func TestVariableSetParsesANestedAssignmentRule(t *testing.T) {
	// A rule is a nested object, which the flat map[string]string accessor
	// cannot represent — so this would silently come back nil if the wrong
	// accessor were used, and the set would look unassigned.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"data":{"id":"varset-1","type":"varsets","attributes":{
			"name":"prod","assignment-rule":{"labels":{"env":"prod"}}}}}`))
	}))
	defer srv.Close()

	vs, err := mustClient(t, srv).GetVariableSet(context.Background(), "varset-1")
	if err != nil {
		t.Fatalf("GetVariableSet: %v", err)
	}
	labels, ok := vs.AssignmentRule["labels"].(map[string]any)
	if !ok || labels["env"] != "prod" {
		t.Fatalf("nested rule lost in parsing: %#v", vs.AssignmentRule)
	}
}

func mustClient(t *testing.T, srv *httptest.Server) *Client {
	t.Helper()
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	return c
}

func TestUpdateAttrsDistinguishesLeaveAloneFromClear(t *testing.T) {
	// The pointer indirection is the whole mechanism: nil means "do not touch
	// the rule", a pointer to a nil map means "remove it". Collapsing the two
	// would make a rule impossible to clear once set — it would silently keep
	// matching workspaces after the operator removed it.
	if attrs := varsetUpdateAttrs(UpdateVariableSetRequest{Name: "x"}); attrs["assignment-rule"] != nil {
		t.Errorf("an omitted rule must not appear in the payload at all: %v", attrs)
	}
	if _, present := varsetUpdateAttrs(UpdateVariableSetRequest{Name: "x"})["assignment-rule"]; present {
		t.Error("an omitted rule must be absent, not present-and-null")
	}

	var cleared map[string]any
	attrs := varsetUpdateAttrs(UpdateVariableSetRequest{Name: "x", AssignmentRule: &cleared})
	if _, present := attrs["assignment-rule"]; !present {
		t.Fatal("clearing must send the key so the server removes the rule")
	}
	// Asserted on the encoded body rather than the in-process value: a nil map
	// is a non-nil `any` holding nil, so a Go nil-check here would fail while
	// the wire — the only thing the server sees — is correct.
	body, err := json.Marshal(attrs)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if !strings.Contains(string(body), `"assignment-rule":null`) {
		t.Errorf("clearing must serialise to null, got %s", body)
	}

	set := map[string]any{"labels": map[string]string{"env": "prod"}}
	attrs = varsetUpdateAttrs(UpdateVariableSetRequest{Name: "x", AssignmentRule: &set})
	if attrs["assignment-rule"] == nil {
		t.Error("a supplied rule must reach the payload")
	}
}

func TestAssociationViewsSurfaceServerErrors(t *testing.T) {
	// A failure here must not read as "this set reaches no workspaces" — that is
	// the answer an operator would act on, and it would be the opposite of
	// unknown.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"errors":[{"detail":"nope"}]}`))
	}))
	defer srv.Close()
	c := mustClient(t, srv)

	if _, err := c.ListVarsetWorkspaces(context.Background(), "varset-1"); err == nil {
		t.Error("ListVarsetWorkspaces swallowed a 403")
	}
	if _, err := c.ListWorkspaceVarsets(context.Background(), "ws-1"); err == nil {
		t.Error("ListWorkspaceVarsets swallowed a 403")
	}
}

func TestGetObjectAttrIsNilSafe(t *testing.T) {
	r := &Resource{Attributes: map[string]json.RawMessage{
		"missing-is-nil": nil,
		"null":           json.RawMessage(`null`),
		"not-an-object":  json.RawMessage(`"a string"`),
		"ok":             json.RawMessage(`{"a":1}`),
	}}
	for _, key := range []string{"absent", "missing-is-nil", "null", "not-an-object"} {
		if got := GetObjectAttr(r, key); got != nil {
			t.Errorf("%s: want nil, got %v", key, got)
		}
	}
	if got := GetObjectAttr(r, "ok"); got == nil || got["a"] != float64(1) {
		t.Errorf("ok: want the parsed object, got %v", got)
	}
}
