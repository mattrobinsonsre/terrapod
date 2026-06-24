package terrapod

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newLabelsFixture(t *testing.T) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/api/terrapod/v1/labels":
			_, _ = w.Write([]byte(`{"data":[
			  {"key":"env","value-count":2,"entity-counts":{"workspaces":5,"agent-pools":1}},
			  {"key":"team","value-count":3,"entity-counts":{"workspaces":4}}
			]}`))
		case r.URL.Path == "/api/terrapod/v1/labels/env":
			_, _ = w.Write([]byte(`{"data":[
			  {"value":"prod","entity-counts":{"workspaces":3}},
			  {"value":"dev","entity-counts":{"workspaces":2}}
			]}`))
		case strings.HasPrefix(r.URL.Path, "/api/terrapod/v1/labels/env/"):
			_, _ = w.Write([]byte(`{"data":{
			  "workspaces":[{"type":"workspaces","id":"ws-1","name":"prod-net","labels":{"env":"prod"}}],
			  "agent-pools":[]
			}}`))
		default:
			http.Error(w, "unhandled", http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestListLabelKeys(t *testing.T) {
	c := newLabelsFixture(t)
	keys, err := c.ListLabelKeys(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(keys) != 2 || keys[0].Key != "env" || keys[0].ValueCount != 2 {
		t.Fatalf("unexpected keys: %+v", keys)
	}
	if keys[0].EntityCounts["workspaces"] != 5 {
		t.Errorf("entity-counts not parsed: %+v", keys[0])
	}
}

func TestListLabelValues(t *testing.T) {
	c := newLabelsFixture(t)
	vals, err := c.ListLabelValues(t.Context(), "env")
	if err != nil {
		t.Fatal(err)
	}
	if len(vals) != 2 || vals[0].Value != "prod" {
		t.Fatalf("unexpected values: %+v", vals)
	}
}

func TestListLabelEntities(t *testing.T) {
	c := newLabelsFixture(t)
	grouped, err := c.ListLabelEntities(t.Context(), "env", "prod")
	if err != nil {
		t.Fatal(err)
	}
	if len(grouped["workspaces"]) != 1 || grouped["workspaces"][0].ID != "ws-1" {
		t.Fatalf("unexpected entities: %+v", grouped)
	}
	if _, ok := grouped["agent-pools"]; !ok {
		t.Errorf("empty type list should be preserved")
	}
}
