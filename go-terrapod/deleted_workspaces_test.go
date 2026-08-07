package terrapod

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newDeletedWorkspaceFixture(t *testing.T) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		p := r.URL.Path
		switch {
		case r.Method == http.MethodGet && strings.HasSuffix(p, "/deleted-workspaces"):
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"11111111-1111-4111-8111-111111111111","type":"deleted-workspaces","attributes":{
			    "workspace-id":"11111111-1111-4111-8111-111111111111",
			    "workspace-name":"prod-network","deleted-at":"2026-08-01T09:00:00Z",
			    "deleted-by":"someone@example.com","marker-reason":"deleted",
			    "last-serial":47,"lineage":"abc-def","state-versions-available":12,
			    "age-days":3.5,"restorable-until":"2026-08-31T09:00:00Z",
			    "settings":{"owner_email":"someone@example.com"},
			    "variable-names":[{"key":"region","category":"terraform","sensitive":false},
			                      {"key":"api_token","category":"env","sensitive":true}]}},
			  {"id":"22222222-2222-4222-8222-222222222222","type":"deleted-workspaces","attributes":{
			    "workspace-id":"22222222-2222-4222-8222-222222222222",
			    "workspace-name":null,"deleted-at":"2026-07-20T09:00:00Z",
			    "marker-reason":"discovered-orphaned","state-versions-available":1,
			    "age-days":null,"restorable-until":"","settings":{},"variable-names":[]}}
			],"meta":{"pagination":{"current-page":1,"page-size":20,"total-pages":1,"total-count":2}}}`))

		case r.Method == http.MethodPost && strings.HasSuffix(p, "/restore"):
			_, _ = w.Write([]byte(`{"data":{"id":"ws-99999999-9999-4999-8999-999999999999","type":"workspaces","attributes":{
			  "name":"prod-network","restored-from":"11111111-1111-4111-8111-111111111111",
			  "state-versions-restored":12,"state-versions-skipped":[],
			  "suppressed":["auto_apply","vcs_connection"],
			  "dropped-references":[{"field":"vcs","connection_id":"c-1","repo_url":"https://example.invalid/r"}]}}}`))

		default:
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"errors":[{"detail":"not found"}]}`))
		}
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	return c
}

func TestListDeletedWorkspaces(t *testing.T) {
	c := newDeletedWorkspaceFixture(t)
	list, err := c.ListDeletedWorkspaces(context.Background(), DeletedWorkspaceListOptions{})
	if err != nil {
		t.Fatalf("ListDeletedWorkspaces: %v", err)
	}
	if len(list.Items) != 2 {
		t.Fatalf("want 2 items, got %d", len(list.Items))
	}
	if list.Meta.TotalCount != 2 {
		t.Errorf("pagination meta not parsed: %+v", list.Meta)
	}

	first := list.Items[0]
	if first.WorkspaceName != "prod-network" || first.LastSerial != 47 {
		t.Errorf("attributes not decoded: %+v", first)
	}
	if first.StateVersionsAvailable != 12 {
		t.Errorf("want 12 state versions available, got %d", first.StateVersionsAvailable)
	}
	if first.AgeDays == nil || *first.AgeDays != 3.5 {
		t.Errorf("age-days not decoded: %+v", first.AgeDays)
	}
}

func TestDeletedWorkspaceVariablesCarryNoValues(t *testing.T) {
	// The marker is a plain object in the bucket and replicates to any
	// standby, so it records variable NAMES only. This pins the shape so a
	// future field cannot quietly start carrying a value.
	c := newDeletedWorkspaceFixture(t)
	list, err := c.ListDeletedWorkspaces(context.Background(), DeletedWorkspaceListOptions{})
	if err != nil {
		t.Fatalf("ListDeletedWorkspaces: %v", err)
	}
	vars := list.Items[0].VariableNames
	if len(vars) != 2 {
		t.Fatalf("want 2 variable names, got %d", len(vars))
	}
	if vars[1].Key != "api_token" || !vars[1].Sensitive {
		t.Errorf("variable name not decoded: %+v", vars[1])
	}
}

func TestDiscoveredOrphanDecodesWithNullFields(t *testing.T) {
	// A reaper-written marker has no name, no deleted-by and no age. Those
	// arrive as JSON null and must not fail the decode — this is exactly the
	// entry an operator most needs to see.
	c := newDeletedWorkspaceFixture(t)
	list, err := c.ListDeletedWorkspaces(context.Background(), DeletedWorkspaceListOptions{})
	if err != nil {
		t.Fatalf("ListDeletedWorkspaces: %v", err)
	}
	orphan := list.Items[1]
	if orphan.MarkerReason != "discovered-orphaned" {
		t.Errorf("want discovered-orphaned, got %q", orphan.MarkerReason)
	}
	if orphan.WorkspaceName != "" || orphan.AgeDays != nil {
		t.Errorf("null fields should decode to zero values: %+v", orphan)
	}
	if orphan.RestorableUntil != "" {
		t.Errorf("retention disabled should leave restorable-until empty, got %q", orphan.RestorableUntil)
	}
}

func TestRestoreDeletedWorkspace(t *testing.T) {
	c := newDeletedWorkspaceFixture(t)
	got, err := c.RestoreDeletedWorkspace(
		context.Background(), "11111111-1111-4111-8111-111111111111", RestoreOptions{})
	if err != nil {
		t.Fatalf("RestoreDeletedWorkspace: %v", err)
	}
	// The restore yields a NEW workspace, so the id must not be the source id.
	if got.ID == "11111111-1111-4111-8111-111111111111" {
		t.Errorf("restore returned the source id, not a new workspace id")
	}
	if got.RestoredFrom != "11111111-1111-4111-8111-111111111111" {
		t.Errorf("restored-from not decoded: %q", got.RestoredFrom)
	}
	if got.StateVersionsRestored != 12 {
		t.Errorf("want 12 restored, got %d", got.StateVersionsRestored)
	}
	// The suppression report is the point of the response: a restored
	// workspace comes back inert and the caller decides what to re-enable.
	if len(got.Suppressed) != 2 || len(got.DroppedReferences) != 1 {
		t.Errorf("suppression report not decoded: %+v / %+v", got.Suppressed, got.DroppedReferences)
	}
}

func TestRestoreOptionsReachTheWire(t *testing.T) {
	// Force is the difference between "refuse, this was already restored" and
	// "do it anyway"; a flag that never leaves the client would silently make
	// the guard unbypassable and look like a server bug.
	var sent map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Data struct {
				Attributes map[string]any `json:"attributes"`
			} `json:"data"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		sent = body.Data.Attributes
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"data":{"id":"ws-x","type":"workspaces","attributes":{}}}`))
	}))
	defer srv.Close()
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}

	if _, err := c.RestoreDeletedWorkspace(
		context.Background(), "abc", RestoreOptions{Name: "recovered", Force: true}); err != nil {
		t.Fatalf("RestoreDeletedWorkspace: %v", err)
	}
	if sent["name"] != "recovered" || sent["force"] != true {
		t.Errorf("options did not reach the wire: %+v", sent)
	}

	// Neither is sent when unset, so the server keeps its own defaults rather
	// than being handed an explicit empty name or an explicit force:false.
	if _, err := c.RestoreDeletedWorkspace(
		context.Background(), "abc", RestoreOptions{}); err != nil {
		t.Fatalf("RestoreDeletedWorkspace: %v", err)
	}
	if len(sent) != 0 {
		t.Errorf("unset options should send nothing, sent %+v", sent)
	}
}

func TestRestoredToDecodes(t *testing.T) {
	c := newDeletedWorkspaceFixture(t)
	list, err := c.ListDeletedWorkspaces(context.Background(), DeletedWorkspaceListOptions{})
	if err != nil {
		t.Fatalf("ListDeletedWorkspaces: %v", err)
	}
	// Absent on a never-restored marker: nil, not a decode failure.
	if list.Items[0].RestoredTo != nil {
		t.Errorf("want nil restored-to on a fresh marker, got %v", list.Items[0].RestoredTo)
	}
}

func TestGetDeletedWorkspaceNotFound(t *testing.T) {
	c := newDeletedWorkspaceFixture(t)
	_, err := c.GetDeletedWorkspace(context.Background(), "does-not-exist")
	if err == nil {
		t.Fatal("want an error for an unknown deleted workspace")
	}
	if !IsNotFound(err) {
		t.Errorf("want a NotFoundError, got %T: %v", err, err)
	}
}
