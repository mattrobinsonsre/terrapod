package terrapod

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newRunTriggerFixture(t *testing.T) (*Client, *[]byte) {
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
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/run-triggers"):
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"rt-aaa","type":"run-triggers","attributes":{
			  "workspace-name":"app","sourceable-name":"network"
			},"relationships":{
			  "workspace":{"data":{"id":"ws-app","type":"workspaces"}},
			  "sourceable":{"data":{"id":"ws-network","type":"workspaces"}}
			}}}`))
		case r.Method == http.MethodGet && r.URL.Path == "/api/terrapod/v1/run-triggers/rt-aaa":
			_, _ = w.Write([]byte(`{"data":{"id":"rt-aaa","type":"run-triggers","relationships":{
			  "workspace":{"data":{"id":"ws-app","type":"workspaces"}},
			  "sourceable":{"data":{"id":"ws-network","type":"workspaces"}}
			}}}`))
		case r.Method == http.MethodGet && strings.Contains(r.URL.Path, "/run-triggers"):
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"rt-aaa","type":"run-triggers","relationships":{
			    "workspace":{"data":{"id":"ws-app","type":"workspaces"}},
			    "sourceable":{"data":{"id":"ws-network","type":"workspaces"}}}}
			]}`))
		case r.Method == http.MethodDelete:
			w.WriteHeader(http.StatusNoContent)
		default:
			http.Error(w, "unhandled "+r.URL.Path, http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c, &lastBody
}

func TestCreateRunTrigger(t *testing.T) {
	c, lastBody := newRunTriggerFixture(t)
	rt, err := c.CreateRunTrigger(t.Context(), CreateRunTriggerRequest{
		DestinationWorkspaceID: "ws-app",
		SourceWorkspaceID:      "ws-network",
	})
	if err != nil {
		t.Fatal(err)
	}
	if rt.ID != "rt-aaa" || rt.WorkspaceID != "ws-app" || rt.SourceID != "ws-network" {
		t.Errorf("run-trigger: %+v", rt)
	}
	// Body shape: sourceable relationship only; no attributes
	var req struct {
		Data struct {
			Relationships map[string]any `json:"relationships"`
		} `json:"data"`
	}
	_ = json.Unmarshal(*lastBody, &req)
	if _, has := req.Data.Relationships["sourceable"]; !has {
		t.Errorf("sourceable relationship missing: %+v", req.Data.Relationships)
	}
}

func TestGetRunTrigger(t *testing.T) {
	c, _ := newRunTriggerFixture(t)
	rt, err := c.GetRunTrigger(t.Context(), "rt-aaa")
	if err != nil {
		t.Fatal(err)
	}
	if rt.SourceID != "ws-network" {
		t.Errorf("source not set: %+v", rt)
	}
}

func TestListInboundRunTriggers(t *testing.T) {
	c, _ := newRunTriggerFixture(t)
	list, err := c.ListInboundRunTriggers(t.Context(), "ws-app")
	if err != nil {
		t.Fatal(err)
	}
	if len(list) != 1 || list[0].SourceID != "ws-network" {
		t.Errorf("list: %+v", list)
	}
}

func TestDeleteRunTrigger(t *testing.T) {
	c, _ := newRunTriggerFixture(t)
	if err := c.DeleteRunTrigger(t.Context(), "rt-aaa"); err != nil {
		t.Error(err)
	}
}
