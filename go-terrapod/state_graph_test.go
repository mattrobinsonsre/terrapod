package terrapod

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

const stateGraphBody = `{"data":{"id":"state-graph","type":"state-graphs","attributes":{
  "nodes":[
    {"id":"aws_vpc.main","kind":"resource","name":"aws_vpc.main","type":"aws_vpc","mode":"managed","module":"(root)","provider":"aws","indeg":1},
    {"id":"module.net.aws_subnet.a","kind":"resource","name":"module.net.aws_subnet.a","type":"aws_subnet","mode":"managed","module":"module.net","provider":"aws","indeg":0}
  ],
  "edges":[
    {"source":"module.net.aws_subnet.a","target":"aws_vpc.main","kind":"depends-on"}
  ],
  "meta":{
    "counts":{"resources":2,"edges":1},
    "truncated":false,
    "total_resources":2,
    "max_nodes":2000,
    "versions":[
      {"id":"sv-2","serial":2,"created_at":"2026-01-02T00:00:00Z","is_current":true},
      {"id":"sv-1","serial":1,"created_at":"2026-01-01T00:00:00Z","is_current":false}
    ],
    "state_version":{"id":"sv-2","serial":2,"created_at":"2026-01-02T00:00:00Z","is_current":true}
  }
}}}`

func TestGetStateGraph(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/terrapod/v1/workspaces/ws-abc/state-graph" {
			http.Error(w, "unexpected path", http.StatusNotFound)
			return
		}
		if r.URL.Query().Get("state_version") != "sv-2" {
			http.Error(w, "missing state_version query", http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(stateGraphBody))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}

	g, err := c.GetStateGraph(t.Context(), "ws-abc", "sv-2")
	if err != nil {
		t.Fatal(err)
	}
	if len(g.Nodes) != 2 || len(g.Edges) != 1 {
		t.Fatalf("nodes=%d edges=%d, want 2/1", len(g.Nodes), len(g.Edges))
	}
	if g.Nodes[0].Type != "aws_vpc" || g.Nodes[0].Module != "(root)" || g.Nodes[0].InDeg != 1 {
		t.Errorf("resource node not decoded: %+v", g.Nodes[0])
	}
	if g.Edges[0].Kind != "depends-on" || g.Edges[0].Target != "aws_vpc.main" {
		t.Errorf("depends-on edge not decoded: %+v", g.Edges[0])
	}
	if g.Meta.Counts["resources"] != 2 || g.Meta.Truncated {
		t.Errorf("meta not decoded: %+v", g.Meta)
	}
	if g.Meta.StateVersion == nil || !g.Meta.StateVersion.IsCurrent || len(g.Meta.Versions) != 2 {
		t.Errorf("version meta not decoded: %+v", g.Meta)
	}
}
