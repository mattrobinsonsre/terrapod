package terrapod

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
)

const settingsJSON = `{"data":{"id":"ws-abc","type":"inventory-settings","attributes":{
  "include-platform":true,"repo-url":"https://example.invalid/org/ansible",
  "branch":"main","working-directory":"inventory","ignore-paths":["archive/"],
  "created-at":"2026-01-01T00:00:00Z","updated-at":"2026-01-01T00:00:00Z"},
  "relationships":{"workspace":{"data":{"id":"ws-abc","type":"workspaces"}},
    "vcs-connection":{"data":{"id":"vcs-9999","type":"vcs-connections"}}}}}`

const settingsNoVCSJSON = `{"data":{"id":"ws-abc","type":"inventory-settings","attributes":{
  "include-platform":true,"repo-url":"","branch":"","working-directory":"",
  "ignore-paths":[],"created-at":"2026-01-01T00:00:00Z",
  "updated-at":"2026-01-01T00:00:00Z"},
  "relationships":{"workspace":{"data":{"id":"ws-abc","type":"workspaces"}},
    "vcs-connection":{"data":null}}}}`

const hostJSON = `{"data":{"id":"invhost-1111","type":"inventory-hosts","attributes":{
  "name":"web-01","group-count":3,"variable-count":2,
  "created-at":"2026-01-01T00:00:00Z","updated-at":"2026-01-01T00:00:00Z"},
  "relationships":{"workspace":{"data":{"id":"ws-abc","type":"workspaces"}}}}}`

const groupJSON = `{"data":{"id":"invgroup-2222","type":"inventory-groups","attributes":{
  "name":"web","member-count":4,"child-count":1,"variable-count":5,
  "created-at":"2026-01-01T00:00:00Z","updated-at":"2026-01-01T00:00:00Z"},
  "relationships":{"workspace":{"data":{"id":"ws-abc","type":"workspaces"}}}}}`

const membershipJSON = `{"data":{"id":"invhg-3333","type":"inventory-host-groups","attributes":{
  "created-at":"2026-01-01T00:00:00Z"},
  "relationships":{"workspace":{"data":{"id":"ws-abc","type":"workspaces"}},
    "host":{"data":{"id":"invhost-1111","type":"inventory-hosts"}},
    "group":{"data":{"id":"invgroup-2222","type":"inventory-groups"}}}}}`

const nestingJSON = `{"data":{"id":"invgc-4444","type":"inventory-group-children","attributes":{
  "created-at":"2026-01-01T00:00:00Z"},
  "relationships":{"workspace":{"data":{"id":"ws-abc","type":"workspaces"}},
    "parent-group":{"data":{"id":"invgroup-2222","type":"inventory-groups"}},
    "child-group":{"data":{"id":"invgroup-5555","type":"inventory-groups"}}}}}`

const hostVarJSON = `{"data":{"id":"invhvar-6666","type":"inventory-host-vars","attributes":{
  "key":"ansible_user","value":"ec2-user","structured":false,"sensitive":false,
  "created-at":"2026-01-01T00:00:00Z","updated-at":"2026-01-01T00:00:00Z"},
  "relationships":{"workspace":{"data":{"id":"ws-abc","type":"workspaces"}},
    "host":{"data":{"id":"invhost-1111","type":"inventory-hosts"}}}}}`

const groupVarJSON = `{"data":{"id":"invgvar-7777","type":"inventory-group-vars","attributes":{
  "key":"http_port","value":"8080","structured":true,"sensitive":false,
  "created-at":"2026-01-01T00:00:00Z","updated-at":"2026-01-01T00:00:00Z"},
  "relationships":{"workspace":{"data":{"id":"ws-abc","type":"workspaces"}},
    "group":{"data":{"id":"invgroup-2222","type":"inventory-groups"}}}}}`

const globalVarJSON = `{"data":{"id":"invvar-8888","type":"inventory-global-vars","attributes":{
  "key":"ansible_become_password","value":"***","structured":false,"sensitive":true,
  "created-at":"2026-01-01T00:00:00Z","updated-at":"2026-01-01T00:00:00Z"},
  "relationships":{"workspace":{"data":{"id":"ws-abc","type":"workspaces"}}}}}`

// The resolved document. Note `edge` has an EMPTY host list while carrying a
// child whose hosts it reaches -- which is exactly ansible's own shape, and the
// thing a reader must not misread as "targets nothing".
const resolvedJSON = `{"data":{"id":"ws-abc","type":"resolved-inventories","attributes":{
  "hosts":{"web-01":{"ansible_host":"10.0.0.4"},"switch1":{}},
  "groups":{"web":["web-01"],"net":["switch1"],"edge":[]},
  "group-children":{"edge":["net"]},
  "host-count":2,"group-count":3},
  "relationships":{"workspace":{"data":{"id":"ws-abc","type":"workspaces"}}}}}`

type invCaptured struct {
	method, path, query string
	resourceType        string
	attrs               map[string]any
	// rels holds each relationship's raw `data`, so a test can tell an
	// explicit null apart from an omitted relationship -- which is the whole
	// distinction a PATCH draws, and invisible if only ids are captured.
	rels map[string]json.RawMessage
}

// relID is the id inside a captured relationship, or "" for null/absent.
func (c *invCaptured) relID(name string) string {
	raw, ok := c.rels[name]
	if !ok || string(raw) == "null" {
		return ""
	}
	var d struct{ ID string }
	if err := json.Unmarshal(raw, &d); err != nil {
		return ""
	}
	return d.ID
}

// relIsExplicitNull says the relationship was sent as `{"data": null}` rather
// than omitted.
func (c *invCaptured) relIsExplicitNull(name string) bool {
	raw, ok := c.rels[name]
	return ok && string(raw) == "null"
}

// inventoryServer answers every request with (status, body) and records it.
func inventoryServer(t *testing.T, status int, body string) (*Client, *invCaptured) {
	t.Helper()
	got := &invCaptured{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got.method, got.path, got.query = r.Method, r.URL.Path, r.URL.RawQuery
		raw, _ := io.ReadAll(r.Body)
		got.attrs, got.rels, got.resourceType = nil, nil, ""
		if len(raw) > 0 {
			var doc struct {
				Data struct {
					Type          string                     `json:"type"`
					Attributes    map[string]any             `json:"attributes"`
					Relationships map[string]json.RawMessage `json:"relationships"`
				} `json:"data"`
			}
			if err := json.Unmarshal(raw, &doc); err != nil {
				t.Errorf("request body is not JSON:API: %s", raw)
			}
			got.attrs = doc.Data.Attributes
			got.resourceType = doc.Data.Type
			got.rels = map[string]json.RawMessage{}
			for name, rel := range doc.Data.Relationships {
				var inner struct {
					Data json.RawMessage `json:"data"`
				}
				if err := json.Unmarshal(rel, &inner); err == nil {
					got.rels[name] = inner.Data
				}
			}
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c, got
}

func invStr(s string) *string { return &s }
func invBool(b bool) *bool    { return &b }

func wantReq(t *testing.T, got *invCaptured, method, path string) {
	t.Helper()
	if got.method != method || got.path != path {
		t.Errorf("wrong request: got %s %s, want %s %s", got.method, got.path, method, path)
	}
}

// ── Settings ─────────────────────────────────────────────────────────────────

func TestGetInventorySettings(t *testing.T) {
	c, got := inventoryServer(t, http.StatusOK, settingsJSON)

	s, err := c.GetInventorySettings(t.Context(), "ws-abc")
	if err != nil {
		t.Fatal(err)
	}
	wantReq(t, got, http.MethodGet, "/api/v1/workspaces/ws-abc/inventory/settings")

	if s.ID != "ws-abc" || s.WorkspaceID != "ws-abc" {
		t.Errorf("settings are identified by the workspace: %+v", s)
	}
	if s.VCSConnectionID != "vcs-9999" {
		t.Errorf("vcs connection: got %q", s.VCSConnectionID)
	}
	if !s.IncludePlatform || s.Branch != "main" || s.WorkingDirectory != "inventory" {
		t.Errorf("attributes not parsed: %+v", s)
	}
	if len(s.IgnorePaths) != 1 || s.IgnorePaths[0] != "archive/" {
		t.Errorf("ignore paths: %v", s.IgnorePaths)
	}
}

func TestGetInventorySettingsWithNoVCSBinding(t *testing.T) {
	c, _ := inventoryServer(t, http.StatusOK, settingsNoVCSJSON)

	s, err := c.GetInventorySettings(t.Context(), "ws-abc")
	if err != nil {
		t.Fatal(err)
	}
	// A null relationship is an UNBOUND source, not a parse failure.
	if s.VCSConnectionID != "" {
		t.Errorf("a null vcs-connection must read as empty, got %q", s.VCSConnectionID)
	}
}

// A workspace with no settings row is the normal default state, so the error a
// caller gets back has to be recognisable as "none" rather than a failure.
func TestGetInventorySettingsIsNotFoundWhenAbsent(t *testing.T) {
	c, _ := inventoryServer(t, http.StatusNotFound,
		`{"errors":[{"detail":"Not found","status":"404"}]}`)

	_, err := c.GetInventorySettings(t.Context(), "ws-abc")
	if err == nil {
		t.Fatal("expected an error")
	}
	var nf *NotFoundError
	if !errors.As(err, &nf) {
		t.Errorf("want a NotFoundError a caller can test for, got %T: %v", err, err)
	}
}

func TestPutInventorySettingsSendsTheWholeState(t *testing.T) {
	c, got := inventoryServer(t, http.StatusOK, settingsJSON)

	_, err := c.PutInventorySettings(t.Context(), "ws-abc", PutInventorySettingsRequest{
		IncludePlatform:  true,
		VCSConnectionID:  "vcs-9999",
		RepoURL:          "https://example.invalid/org/ansible",
		Branch:           "main",
		WorkingDirectory: "inventory",
		IgnorePaths:      []string{"archive/"},
	})
	if err != nil {
		t.Fatal(err)
	}
	wantReq(t, got, http.MethodPut, "/api/v1/workspaces/ws-abc/inventory/settings")

	// A PUT is a replace, so every attribute is present even at its default --
	// an omitted one would be indistinguishable from "leave it alone", which
	// is what the PATCH is for.
	for _, key := range []string{
		"include-platform", "repo-url", "branch", "working-directory", "ignore-paths",
	} {
		if _, ok := got.attrs[key]; !ok {
			t.Errorf("a PUT must send %q even at its default", key)
		}
	}
	if got.relID("vcs-connection") != "vcs-9999" {
		t.Errorf("vcs-connection relationship: %q", got.relID("vcs-connection"))
	}
}

func TestPutInventorySettingsWithNoConnectionSendsExplicitNull(t *testing.T) {
	c, got := inventoryServer(t, http.StatusOK, settingsNoVCSJSON)

	_, err := c.PutInventorySettings(t.Context(), "ws-abc", PutInventorySettingsRequest{
		IncludePlatform: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	// Omitting the relationship would leave a previously-bound source in
	// place, which is the opposite of what a replace means.
	if !got.relIsExplicitNull("vcs-connection") {
		t.Errorf("a PUT with no connection must send data:null, got %v",
			got.rels["vcs-connection"])
	}
}

func TestUpdateInventorySettingsOmitsWhatItIsNotChanging(t *testing.T) {
	c, got := inventoryServer(t, http.StatusOK, settingsJSON)

	_, err := c.UpdateInventorySettings(t.Context(), "ws-abc",
		UpdateInventorySettingsRequest{Branch: invStr("release")})
	if err != nil {
		t.Fatal(err)
	}
	wantReq(t, got, http.MethodPatch, "/api/v1/workspaces/ws-abc/inventory/settings")

	if got.attrs["branch"] != "release" {
		t.Errorf("branch not sent: %v", got.attrs)
	}
	for _, key := range []string{
		"include-platform", "repo-url", "working-directory", "ignore-paths",
	} {
		if _, ok := got.attrs[key]; ok {
			t.Errorf("a patch must not send %q it was not given", key)
		}
	}
	// No relationship at all, so the binding is left alone.
	if _, ok := got.rels["vcs-connection"]; ok {
		t.Errorf("an untouched binding must send no relationship, got %v",
			got.rels["vcs-connection"])
	}
}

func TestUpdateInventorySettingsClearsTheBindingWithAnExplicitNull(t *testing.T) {
	c, got := inventoryServer(t, http.StatusOK, settingsNoVCSJSON)

	_, err := c.UpdateInventorySettings(t.Context(), "ws-abc",
		UpdateInventorySettingsRequest{ClearVCSConnection: true})
	if err != nil {
		t.Fatal(err)
	}
	if !got.relIsExplicitNull("vcs-connection") {
		t.Errorf("clearing needs data:null, got %v", got.rels["vcs-connection"])
	}
}

func TestUpdateInventorySettingsClearsIgnorePathsWithAnEmptyList(t *testing.T) {
	c, got := inventoryServer(t, http.StatusOK, settingsNoVCSJSON)

	// A pointer to a NIL slice means "clear", so it has to arrive as `[]`.
	// Dereferencing straight into the body marshals as JSON null, which the
	// server reads as absent -- so the one request that removes every ignore
	// path would silently do nothing.
	var none []string
	_, err := c.UpdateInventorySettings(t.Context(), "ws-abc",
		UpdateInventorySettingsRequest{IgnorePaths: &none})
	if err != nil {
		t.Fatal(err)
	}
	paths, ok := got.attrs["ignore-paths"]
	if !ok {
		t.Fatal("ignore-paths was not sent at all")
	}
	list, ok := paths.([]any)
	if !ok || len(list) != 0 {
		t.Errorf("want an empty list, got %#v", paths)
	}
}

func TestDeleteInventorySettings(t *testing.T) {
	c, got := inventoryServer(t, http.StatusNoContent, "")

	if err := c.DeleteInventorySettings(t.Context(), "ws-abc"); err != nil {
		t.Fatal(err)
	}
	wantReq(t, got, http.MethodDelete, "/api/v1/workspaces/ws-abc/inventory/settings")
}

// ── Hosts ────────────────────────────────────────────────────────────────────

func TestCreateInventoryHost(t *testing.T) {
	c, got := inventoryServer(t, http.StatusCreated, hostJSON)

	host, err := c.CreateInventoryHost(t.Context(), "ws-abc", "web-01")
	if err != nil {
		t.Fatal(err)
	}
	wantReq(t, got, http.MethodPost, "/api/v1/workspaces/ws-abc/inventory/hosts")

	if got.resourceType != "inventory-hosts" {
		t.Errorf("resource type: %q", got.resourceType)
	}
	if got.attrs["name"] != "web-01" {
		t.Errorf("name not sent: %v", got.attrs)
	}
	if host.ID != "invhost-1111" || host.Name != "web-01" {
		t.Errorf("response not parsed: %+v", host)
	}
	// Counts, not the rows -- a host list must not grow with the inventory.
	if host.GroupCount != 3 || host.VariableCount != 2 {
		t.Errorf("counts not parsed: %+v", host)
	}
	if host.WorkspaceID != "ws-abc" {
		t.Errorf("workspace relationship: %q", host.WorkspaceID)
	}
}

func TestGetUpdateDeleteInventoryHost(t *testing.T) {
	t.Run("get", func(t *testing.T) {
		c, got := inventoryServer(t, http.StatusOK, hostJSON)
		if _, err := c.GetInventoryHost(t.Context(), "invhost-1111"); err != nil {
			t.Fatal(err)
		}
		wantReq(t, got, http.MethodGet, "/api/v1/inventory-hosts/invhost-1111")
	})
	t.Run("rename", func(t *testing.T) {
		c, got := inventoryServer(t, http.StatusOK, hostJSON)
		if _, err := c.UpdateInventoryHost(t.Context(), "invhost-1111", "web-02"); err != nil {
			t.Fatal(err)
		}
		wantReq(t, got, http.MethodPatch, "/api/v1/inventory-hosts/invhost-1111")
		if got.attrs["name"] != "web-02" {
			t.Errorf("name not sent: %v", got.attrs)
		}
	})
	t.Run("delete", func(t *testing.T) {
		c, got := inventoryServer(t, http.StatusNoContent, "")
		if err := c.DeleteInventoryHost(t.Context(), "invhost-1111"); err != nil {
			t.Fatal(err)
		}
		wantReq(t, got, http.MethodDelete, "/api/v1/inventory-hosts/invhost-1111")
	})
}

func TestListInventoryHosts(t *testing.T) {
	body := `{"data":[{"id":"invhost-1111","type":"inventory-hosts",
	  "attributes":{"name":"web-01","group-count":1,"variable-count":0}}],
	  "meta":{"pagination":{"current-page":1,"page-size":20,"total-count":1,"total-pages":1}}}`
	c, got := inventoryServer(t, http.StatusOK, body)

	hosts, err := c.ListInventoryHosts(t.Context(), "ws-abc")
	if err != nil {
		t.Fatal(err)
	}
	wantReq(t, got, http.MethodGet, "/api/v1/workspaces/ws-abc/inventory/hosts")
	if len(hosts) != 1 || hosts[0].Name != "web-01" {
		t.Errorf("list not parsed: %+v", hosts)
	}
}

// ListAll must page, because a `for_each` over a few hundred instances is the
// documented shape for declaring hosts and one page holds a hundred.
func TestListAllInventoryHostsPages(t *testing.T) {
	var pages []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		pages = append(pages, r.URL.Query().Get("page[number]"))
		page := r.URL.Query().Get("page[number]")
		id := "invhost-a"
		if page == "2" {
			id = "invhost-b"
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = fmt.Fprintf(w, `{"data":[{"id":%q,"type":"inventory-hosts",
		  "attributes":{"name":"h-%s"}}],
		  "meta":{"pagination":{"current-page":%s,"page-size":100,
		    "total-count":2,"total-pages":2}}}`, id, page, page)
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}

	hosts, err := c.ListAllInventoryHosts(t.Context(), "ws-abc")
	if err != nil {
		t.Fatal(err)
	}
	if len(pages) != 2 || pages[0] != "1" || pages[1] != "2" {
		t.Errorf("want two pages requested in order, got %v", pages)
	}
	if len(hosts) != 2 {
		t.Errorf("want both pages' hosts, got %d", len(hosts))
	}
}

// ── Groups ───────────────────────────────────────────────────────────────────

func TestCreateInventoryGroup(t *testing.T) {
	c, got := inventoryServer(t, http.StatusCreated, groupJSON)

	group, err := c.CreateInventoryGroup(t.Context(), "ws-abc", "web")
	if err != nil {
		t.Fatal(err)
	}
	wantReq(t, got, http.MethodPost, "/api/v1/workspaces/ws-abc/inventory/groups")
	if got.attrs["name"] != "web" {
		t.Errorf("name not sent: %v", got.attrs)
	}
	if group.MemberCount != 4 || group.ChildCount != 1 || group.VariableCount != 5 {
		t.Errorf("counts not parsed: %+v", group)
	}
}

func TestGetUpdateDeleteInventoryGroup(t *testing.T) {
	t.Run("get", func(t *testing.T) {
		c, got := inventoryServer(t, http.StatusOK, groupJSON)
		if _, err := c.GetInventoryGroup(t.Context(), "invgroup-2222"); err != nil {
			t.Fatal(err)
		}
		wantReq(t, got, http.MethodGet, "/api/v1/inventory-groups/invgroup-2222")
	})
	t.Run("rename", func(t *testing.T) {
		c, got := inventoryServer(t, http.StatusOK, groupJSON)
		if _, err := c.UpdateInventoryGroup(t.Context(), "invgroup-2222", "frontend"); err != nil {
			t.Fatal(err)
		}
		wantReq(t, got, http.MethodPatch, "/api/v1/inventory-groups/invgroup-2222")
		if got.attrs["name"] != "frontend" {
			t.Errorf("name not sent: %v", got.attrs)
		}
	})
	t.Run("delete", func(t *testing.T) {
		c, got := inventoryServer(t, http.StatusNoContent, "")
		if err := c.DeleteInventoryGroup(t.Context(), "invgroup-2222"); err != nil {
			t.Fatal(err)
		}
		wantReq(t, got, http.MethodDelete, "/api/v1/inventory-groups/invgroup-2222")
	})
}

func TestListAllInventoryGroupsSharesThePagingLoop(t *testing.T) {
	c, got := inventoryServer(t, http.StatusOK,
		`{"data":[],"meta":{"pagination":{"current-page":1,"page-size":100,
		  "total-count":0,"total-pages":1}}}`)

	if _, err := c.ListAllInventoryGroups(t.Context(), "ws-abc"); err != nil {
		t.Fatal(err)
	}
	wantReq(t, got, http.MethodGet, "/api/v1/workspaces/ws-abc/inventory/groups")
	if got.query == "" {
		t.Error("ListAll must send paging parameters")
	}
}

// ── Membership: the two sides are the same row, created at different paths ────

func TestAddHostToInventoryGroup(t *testing.T) {
	c, got := inventoryServer(t, http.StatusCreated, membershipJSON)

	link, err := c.AddHostToInventoryGroup(t.Context(), "invgroup-2222", "invhost-1111")
	if err != nil {
		t.Fatal(err)
	}
	// The group is in the path and the HOST in a relationship.
	wantReq(t, got, http.MethodPost, "/api/v1/inventory-groups/invgroup-2222/hosts")
	if got.relID("host") != "invhost-1111" {
		t.Errorf("host relationship: %q", got.relID("host"))
	}
	if _, ok := got.rels["group"]; ok {
		t.Error("the group comes from the path; sending it too is redundant")
	}
	if link.HostID != "invhost-1111" || link.GroupID != "invgroup-2222" {
		t.Errorf("both sides must parse: %+v", link)
	}
}

func TestAddInventoryGroupToHost(t *testing.T) {
	c, got := inventoryServer(t, http.StatusCreated, membershipJSON)

	if _, err := c.AddInventoryGroupToHost(
		t.Context(), "invhost-1111", "invgroup-2222"); err != nil {
		t.Fatal(err)
	}
	// Mirror image: the host is in the path and the GROUP in a relationship.
	// Getting these the wrong way round would address an existing route with
	// a relationship it does not read, which answers 422 rather than failing
	// to compile -- so the pairing is asserted rather than assumed.
	wantReq(t, got, http.MethodPost, "/api/v1/inventory-hosts/invhost-1111/groups")
	if got.relID("group") != "invgroup-2222" {
		t.Errorf("group relationship: %q", got.relID("group"))
	}
	if _, ok := got.rels["host"]; ok {
		t.Error("the host comes from the path")
	}
}

func TestGetListDeleteInventoryHostGroup(t *testing.T) {
	t.Run("get", func(t *testing.T) {
		c, got := inventoryServer(t, http.StatusOK, membershipJSON)
		if _, err := c.GetInventoryHostGroup(t.Context(), "invhg-3333"); err != nil {
			t.Fatal(err)
		}
		wantReq(t, got, http.MethodGet, "/api/v1/inventory-host-groups/invhg-3333")
	})
	t.Run("members of a group", func(t *testing.T) {
		c, got := inventoryServer(t, http.StatusOK, `{"data":[]}`)
		if _, err := c.ListInventoryGroupMembers(t.Context(), "invgroup-2222"); err != nil {
			t.Fatal(err)
		}
		wantReq(t, got, http.MethodGet, "/api/v1/inventory-groups/invgroup-2222/hosts")
	})
	t.Run("memberships of a host", func(t *testing.T) {
		c, got := inventoryServer(t, http.StatusOK, `{"data":[]}`)
		if _, err := c.ListInventoryHostMemberships(t.Context(), "invhost-1111"); err != nil {
			t.Fatal(err)
		}
		wantReq(t, got, http.MethodGet, "/api/v1/inventory-hosts/invhost-1111/groups")
	})
	t.Run("delete", func(t *testing.T) {
		c, got := inventoryServer(t, http.StatusNoContent, "")
		if err := c.DeleteInventoryHostGroup(t.Context(), "invhg-3333"); err != nil {
			t.Fatal(err)
		}
		wantReq(t, got, http.MethodDelete, "/api/v1/inventory-host-groups/invhg-3333")
	})
}

// ── Nesting: also two sides, and the relationship names differ ───────────────

func TestAddInventoryGroupChild(t *testing.T) {
	c, got := inventoryServer(t, http.StatusCreated, nestingJSON)

	link, err := c.AddInventoryGroupChild(t.Context(), "invgroup-2222", "invgroup-5555")
	if err != nil {
		t.Fatal(err)
	}
	wantReq(t, got, http.MethodPost, "/api/v1/inventory-groups/invgroup-2222/children")
	if got.relID("child-group") != "invgroup-5555" {
		t.Errorf("child-group relationship: %q", got.relID("child-group"))
	}
	if link.ParentGroupID != "invgroup-2222" || link.ChildGroupID != "invgroup-5555" {
		t.Errorf("both ends must parse, and not be swapped: %+v", link)
	}
}

func TestAddInventoryGroupParent(t *testing.T) {
	c, got := inventoryServer(t, http.StatusCreated, nestingJSON)

	// A group may have several parents, so this direction is as natural as the
	// other -- and it sends `parent-group`, not `child-group`.
	if _, err := c.AddInventoryGroupParent(
		t.Context(), "invgroup-5555", "invgroup-2222"); err != nil {
		t.Fatal(err)
	}
	wantReq(t, got, http.MethodPost, "/api/v1/inventory-groups/invgroup-5555/parents")
	if got.relID("parent-group") != "invgroup-2222" {
		t.Errorf("parent-group relationship: %q", got.relID("parent-group"))
	}
	if _, ok := got.rels["child-group"]; ok {
		t.Error("the child comes from the path on this side")
	}
}

func TestGetListDeleteInventoryGroupChild(t *testing.T) {
	t.Run("get", func(t *testing.T) {
		c, got := inventoryServer(t, http.StatusOK, nestingJSON)
		if _, err := c.GetInventoryGroupChild(t.Context(), "invgc-4444"); err != nil {
			t.Fatal(err)
		}
		wantReq(t, got, http.MethodGet, "/api/v1/inventory-group-children/invgc-4444")
	})
	t.Run("children", func(t *testing.T) {
		c, got := inventoryServer(t, http.StatusOK, `{"data":[]}`)
		if _, err := c.ListInventoryGroupChildren(t.Context(), "invgroup-2222"); err != nil {
			t.Fatal(err)
		}
		wantReq(t, got, http.MethodGet, "/api/v1/inventory-groups/invgroup-2222/children")
	})
	t.Run("parents", func(t *testing.T) {
		c, got := inventoryServer(t, http.StatusOK, `{"data":[]}`)
		if _, err := c.ListInventoryGroupParents(t.Context(), "invgroup-2222"); err != nil {
			t.Fatal(err)
		}
		wantReq(t, got, http.MethodGet, "/api/v1/inventory-groups/invgroup-2222/parents")
	})
	t.Run("delete", func(t *testing.T) {
		c, got := inventoryServer(t, http.StatusNoContent, "")
		if err := c.DeleteInventoryGroupChild(t.Context(), "invgc-4444"); err != nil {
			t.Fatal(err)
		}
		wantReq(t, got, http.MethodDelete, "/api/v1/inventory-group-children/invgc-4444")
	})
}

// ── Variables: three kinds, one struct, three sets of routes ─────────────────

func TestCreateInventoryHostVar(t *testing.T) {
	c, got := inventoryServer(t, http.StatusCreated, hostVarJSON)

	v, err := c.CreateInventoryHostVar(t.Context(), "invhost-1111",
		CreateInventoryVarRequest{Key: "ansible_user", Value: "ec2-user"})
	if err != nil {
		t.Fatal(err)
	}
	wantReq(t, got, http.MethodPost, "/api/v1/inventory-hosts/invhost-1111/vars")
	if got.resourceType != "inventory-host-vars" {
		t.Errorf("resource type: %q", got.resourceType)
	}
	// Every field goes on a create, including the two booleans at false: an
	// omitted `structured` would be indistinguishable from "leave it alone",
	// and a create has nothing to leave alone.
	for _, key := range []string{"key", "value", "structured", "sensitive"} {
		if _, ok := got.attrs[key]; !ok {
			t.Errorf("a create must send %q", key)
		}
	}
	if v.HostID != "invhost-1111" || v.GroupID != "" {
		t.Errorf("a host variable carries only its host: %+v", v)
	}
}

func TestCreateInventoryGroupVar(t *testing.T) {
	c, got := inventoryServer(t, http.StatusCreated, groupVarJSON)

	v, err := c.CreateInventoryGroupVar(t.Context(), "invgroup-2222",
		CreateInventoryVarRequest{Key: "http_port", Value: "8080", Structured: true})
	if err != nil {
		t.Fatal(err)
	}
	wantReq(t, got, http.MethodPost, "/api/v1/inventory-groups/invgroup-2222/vars")
	if got.resourceType != "inventory-group-vars" {
		t.Errorf("resource type: %q", got.resourceType)
	}
	if got.attrs["structured"] != true {
		t.Errorf("structured not sent: %v", got.attrs)
	}
	if v.GroupID != "invgroup-2222" || v.HostID != "" {
		t.Errorf("a group variable carries only its group: %+v", v)
	}
	if !v.Structured {
		t.Error("structured not parsed")
	}
}

func TestCreateInventoryGlobalVar(t *testing.T) {
	c, got := inventoryServer(t, http.StatusCreated, globalVarJSON)

	v, err := c.CreateInventoryGlobalVar(t.Context(), "ws-abc",
		CreateInventoryVarRequest{
			Key: "ansible_become_password", Value: "s3cret", Sensitive: true,
		})
	if err != nil {
		t.Fatal(err)
	}
	// Parented on the WORKSPACE, because `all` cannot be a group.
	wantReq(t, got, http.MethodPost, "/api/v1/workspaces/ws-abc/inventory/vars")
	if got.resourceType != "inventory-global-vars" {
		t.Errorf("resource type: %q", got.resourceType)
	}
	if v.HostID != "" || v.GroupID != "" {
		t.Errorf("a variable under `all` has neither parent: %+v", v)
	}
	if v.WorkspaceID != "ws-abc" {
		t.Errorf("workspace: %q", v.WorkspaceID)
	}
}

// A sensitive value never comes back. A caller that wrote back what it read
// would store the mask as the secret, so MaskedValue exists to be compared
// against rather than a literal spelled out at each call site.
func TestASensitiveValueReadsBackMasked(t *testing.T) {
	c, _ := inventoryServer(t, http.StatusOK, globalVarJSON)

	v, err := c.GetInventoryGlobalVar(t.Context(), "invvar-8888")
	if err != nil {
		t.Fatal(err)
	}
	if !v.Sensitive {
		t.Fatal("sensitive not parsed")
	}
	if v.Value != MaskedValue {
		t.Errorf("want the mask, got %q", v.Value)
	}
}

func TestUpdateInventoryVarOmitsWhatItIsNotChanging(t *testing.T) {
	c, got := inventoryServer(t, http.StatusOK, hostVarJSON)

	// Setting `sensitive` alone, without resending a value that may have been
	// read back masked, is the case the pointers exist for.
	_, err := c.UpdateInventoryHostVar(t.Context(), "invhvar-6666",
		UpdateInventoryVarRequest{Sensitive: invBool(true)})
	if err != nil {
		t.Fatal(err)
	}
	wantReq(t, got, http.MethodPatch, "/api/v1/inventory-host-vars/invhvar-6666")
	if got.attrs["sensitive"] != true {
		t.Errorf("sensitive not sent: %v", got.attrs)
	}
	for _, key := range []string{"key", "value", "structured"} {
		if _, ok := got.attrs[key]; ok {
			t.Errorf("a patch must not send %q it was not given", key)
		}
	}
}

func TestUpdateInventoryVarCanRenameTheKey(t *testing.T) {
	c, got := inventoryServer(t, http.StatusOK, hostVarJSON)

	_, err := c.UpdateInventoryGroupVar(t.Context(), "invgvar-7777",
		UpdateInventoryVarRequest{Key: invStr("https_port")})
	if err != nil {
		t.Fatal(err)
	}
	wantReq(t, got, http.MethodPatch, "/api/v1/inventory-group-vars/invgvar-7777")
	if got.attrs["key"] != "https_port" {
		t.Errorf("key not sent: %v", got.attrs)
	}
}

func TestVariableListsAndDeletesAddressTheirOwnRoutes(t *testing.T) {
	cases := []struct {
		name   string
		call   func(*Client) error
		method string
		path   string
	}{
		{"list host vars", func(c *Client) error {
			_, err := c.ListInventoryHostVars(t.Context(), "invhost-1111")
			return err
		}, http.MethodGet, "/api/v1/inventory-hosts/invhost-1111/vars"},
		{"list group vars", func(c *Client) error {
			_, err := c.ListInventoryGroupVars(t.Context(), "invgroup-2222")
			return err
		}, http.MethodGet, "/api/v1/inventory-groups/invgroup-2222/vars"},
		{"list vars under all", func(c *Client) error {
			_, err := c.ListInventoryGlobalVars(t.Context(), "ws-abc")
			return err
		}, http.MethodGet, "/api/v1/workspaces/ws-abc/inventory/vars"},
		{"get host var", func(c *Client) error {
			_, err := c.GetInventoryHostVar(t.Context(), "invhvar-6666")
			return err
		}, http.MethodGet, "/api/v1/inventory-host-vars/invhvar-6666"},
		{"get group var", func(c *Client) error {
			_, err := c.GetInventoryGroupVar(t.Context(), "invgvar-7777")
			return err
		}, http.MethodGet, "/api/v1/inventory-group-vars/invgvar-7777"},
		{"delete host var", func(c *Client) error {
			return c.DeleteInventoryHostVar(t.Context(), "invhvar-6666")
		}, http.MethodDelete, "/api/v1/inventory-host-vars/invhvar-6666"},
		{"delete group var", func(c *Client) error {
			return c.DeleteInventoryGroupVar(t.Context(), "invgvar-7777")
		}, http.MethodDelete, "/api/v1/inventory-group-vars/invgvar-7777"},
		{"delete var under all", func(c *Client) error {
			return c.DeleteInventoryGlobalVar(t.Context(), "invvar-8888")
		}, http.MethodDelete, "/api/v1/inventory-global-vars/invvar-8888"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			body := hostVarJSON
			if tc.method == http.MethodGet {
				body = `{"data":[]}`
				if tc.path == "/api/v1/inventory-host-vars/invhvar-6666" ||
					tc.path == "/api/v1/inventory-group-vars/invgvar-7777" {
					body = hostVarJSON
				}
			}
			c, got := inventoryServer(t, http.StatusOK, body)
			if err := tc.call(c); err != nil {
				t.Fatal(err)
			}
			wantReq(t, got, tc.method, tc.path)
		})
	}
}

// ── Resolution ───────────────────────────────────────────────────────────────

func TestGetResolvedInventory(t *testing.T) {
	c, got := inventoryServer(t, http.StatusOK, resolvedJSON)

	r, err := c.GetResolvedInventory(t.Context(), "ws-abc")
	if err != nil {
		t.Fatal(err)
	}
	wantReq(t, got, http.MethodGet, "/api/v1/workspaces/ws-abc/inventory/resolved")
	if got.query != "" {
		t.Errorf("no limit means no query: %q", got.query)
	}

	if r.HostCount != 2 || r.GroupCount != 3 {
		t.Errorf("counts: %+v", r)
	}
	if r.Groups["web"][0] != "web-01" || r.Groups["net"][0] != "switch1" {
		t.Errorf("groups not parsed: %v", r.Groups)
	}
	// A parent whose members all arrive through a child reports an EMPTY host
	// list of its own -- ansible's own shape. The nesting is what says it
	// reaches anything, so both have to survive the parse.
	if len(r.Groups["edge"]) != 0 {
		t.Errorf("a nesting-only parent has no direct members: %v", r.Groups["edge"])
	}
	if len(r.GroupChildren["edge"]) != 1 || r.GroupChildren["edge"][0] != "net" {
		t.Errorf("group-children must carry the nesting: %v", r.GroupChildren)
	}
}

// A host with no variables is omitted from ansible's own `_meta.hostvars`, so
// the server builds Hosts from the membership lists instead. Losing it here
// would silently shrink every target set derived from this field.
func TestAVarlessHostSurvivesResolution(t *testing.T) {
	c, _ := inventoryServer(t, http.StatusOK, resolvedJSON)

	r, err := c.GetResolvedInventory(t.Context(), "ws-abc")
	if err != nil {
		t.Fatal(err)
	}
	vars, ok := r.Hosts["switch1"]
	if !ok {
		t.Fatal("a host with no variables was dropped from Hosts")
	}
	if len(vars) != 0 {
		t.Errorf("want an empty variable map, got %v", vars)
	}
	if r.Hosts["web-01"]["ansible_host"] != "10.0.0.4" {
		t.Errorf("host variables not parsed: %v", r.Hosts["web-01"])
	}
}

func TestGetResolvedInventoryWithLimit(t *testing.T) {
	body := `{"data":{"id":"ws-abc","type":"resolved-inventories","attributes":{
	  "hosts":{"web-01":{}},"groups":{"web":["web-01"]},"group-children":{},
	  "host-count":1,"group-count":1,"limit":"~^web"},
	  "relationships":{"workspace":{"data":{"id":"ws-abc","type":"workspaces"}}}}}`
	c, got := inventoryServer(t, http.StatusOK, body)

	// `~regex` is ansible's own term and reaches it untouched -- an earlier
	// implementation refused it, which is the behaviour this inverts.
	r, err := c.GetResolvedInventoryWithLimit(t.Context(), "ws-abc", "~^web")
	if err != nil {
		t.Fatal(err)
	}
	wantReq(t, got, http.MethodGet, "/api/v1/workspaces/ws-abc/inventory/resolved")
	// Assert the pattern ARRIVES INTACT rather than asserting a particular
	// escaping: Go leaves `~` unescaped and escapes `^`, which is RFC 3986's
	// table and not ours to pin. What matters is that the server decodes back
	// exactly what the caller asked for, operators and all.
	q, err := url.ParseQuery(got.query)
	if err != nil {
		t.Fatalf("query is not parseable: %q", got.query)
	}
	if q.Get("limit") != "~^web" {
		t.Errorf("the limit must arrive intact, got %q from %q", q.Get("limit"), got.query)
	}
	if r.Limit != "~^web" {
		t.Errorf("the limit must be echoed back: %q", r.Limit)
	}
}

// Resolution fails closed, so a failure has to surface rather than yielding an
// empty host set: a configure targeting too little is worse than no answer.
func TestResolutionFailureIsAnError(t *testing.T) {
	c, _ := inventoryServer(t, http.StatusServiceUnavailable,
		`{"errors":[{"detail":"ansible could not be obtained","status":"503"}]}`)

	r, err := c.GetResolvedInventory(t.Context(), "ws-abc")
	if err == nil {
		t.Fatal("a failed resolution must not return a host set")
	}
	if r != nil {
		t.Errorf("want no resolution alongside the error, got %+v", r)
	}
}
