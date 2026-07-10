package terrapod

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func newImpactGraphFixture(t *testing.T, status int, body string) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		if status != http.StatusOK {
			w.WriteHeader(status)
			_, _ = w.Write([]byte(`{"errors":[{"detail":"no plan graph for this run"}]}`))
			return
		}
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

const impactGraphBody = `{"data":{"id":"impact-graph-abc","type":"impact-graphs",
  "attributes":{
    "nodes":[
      {"id":"module.net.random_id.vpc","type":"random_id","name":"vpc","provider":"random","action":"create","key":null,"module":"net"},
      {"id":"module.app.null_resource.svc[\"api\"]","type":"null_resource","name":"svc","provider":"null","action":"create","key":"api","module":"app"}
    ],
    "edges":[
      {"source":"module.app.null_resource.svc[\"api\"]","target":"module.net.random_id.vpc"}
    ],
    "meta":{"terraform_version":"1.12.3","counts":{"create":2,"update":0,"replace":0,"delete":0,"noop":0}}
  },
  "relationships":{"run":{"data":{"id":"run-abc","type":"runs"}}}}}`

func TestGetRunImpactGraph(t *testing.T) {
	c := newImpactGraphFixture(t, http.StatusOK, impactGraphBody)
	g, err := c.GetRunImpactGraph(t.Context(), "abc") // bare UUID → "run-abc"
	if err != nil {
		t.Fatal(err)
	}
	if g.RunID != "abc" {
		t.Errorf("RunID = %q, want abc (prefix stripped)", g.RunID)
	}
	if len(g.Nodes) != 2 || len(g.Edges) != 1 {
		t.Fatalf("nodes=%d edges=%d, want 2/1", len(g.Nodes), len(g.Edges))
	}
	if g.Nodes[0].Module != "net" || g.Nodes[1].Module != "app" {
		t.Errorf("module paths not decoded: %+v", g.Nodes)
	}
	if g.Nodes[1].Key == nil || *g.Nodes[1].Key != "api" {
		t.Errorf("for_each key not decoded: %+v", g.Nodes[1].Key)
	}
	if g.Edges[0].Source != `module.app.null_resource.svc["api"]` ||
		g.Edges[0].Target != "module.net.random_id.vpc" {
		t.Errorf("cross-module edge not decoded: %+v", g.Edges[0])
	}
	if g.Meta.TerraformVersion != "1.12.3" || g.Meta.Counts["create"] != 2 {
		t.Errorf("meta not decoded: %+v", g.Meta)
	}
}

func TestGetRunImpactGraphNotFound(t *testing.T) {
	c := newImpactGraphFixture(t, http.StatusNotFound, "")
	_, err := c.GetRunImpactGraph(t.Context(), "run-none")
	if !IsNotFound(err) {
		t.Fatalf("want NotFoundError, got %v", err)
	}
}

func TestGetRunImpactGraphEmptyID(t *testing.T) {
	c := newImpactGraphFixture(t, http.StatusOK, impactGraphBody)
	if _, err := c.GetRunImpactGraph(t.Context(), ""); err == nil {
		t.Fatal("want error for empty run id")
	}
}
