package terrapod

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

const estateGraphBody = `{"data":{"id":"estate-graph","type":"estate-graphs","attributes":{
  "nodes":[
    {"id":"ws-1","kind":"workspace","name":"vpc-core","labels":{"team":"platform"},"pool":"aws-use1","indeg":3},
    {"id":"ws-2","kind":"workspace","name":"app-web","labels":{"team":"web"},"pool":"(local)","indeg":0},
    {"id":"mod-9","kind":"module","name":"vpc/aws","labels":{},"pool":"","indeg":0}
  ],
  "edges":[
    {"source":"ws-2","target":"ws-1","kind":"remote-state"},
    {"source":"mod-9","target":"ws-1","kind":"uses-module"}
  ],
  "meta":{"counts":{"workspaces":2,"modules":1,"edges":2}}
}}}`

func TestGetEstateGraph(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/terrapod/v1/estate-graph" {
			http.Error(w, "unexpected path", http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(estateGraphBody))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}

	g, err := c.GetEstateGraph(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(g.Nodes) != 3 || len(g.Edges) != 2 {
		t.Fatalf("nodes=%d edges=%d, want 3/2", len(g.Nodes), len(g.Edges))
	}
	if g.Nodes[0].Kind != "workspace" || g.Nodes[0].Labels["team"] != "platform" ||
		g.Nodes[0].Pool != "aws-use1" || g.Nodes[0].InDeg != 3 {
		t.Errorf("workspace node not decoded: %+v", g.Nodes[0])
	}
	if g.Nodes[2].Kind != "module" || g.Nodes[2].Name != "vpc/aws" {
		t.Errorf("module node not decoded: %+v", g.Nodes[2])
	}
	if g.Edges[1].Kind != "uses-module" || g.Edges[1].Source != "mod-9" {
		t.Errorf("uses-module edge not decoded: %+v", g.Edges[1])
	}
	if g.Meta.Counts["workspaces"] != 2 || g.Meta.Counts["edges"] != 2 {
		t.Errorf("meta counts not decoded: %+v", g.Meta.Counts)
	}
}
