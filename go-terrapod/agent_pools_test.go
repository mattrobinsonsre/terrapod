package terrapod

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newPoolFixture(t *testing.T) (*Client, *[]byte) {
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
		case r.Method == http.MethodPost && r.URL.Path == "/api/terrapod/v1/agent-pools":
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"apool-aaa","type":"agent-pools","attributes":{
			  "name":"aws-prod","description":"AWS prod runners",
			  "labels":{"region":"us-east-1","env":"prod"},
			  "owner-email":"sre@example.com"
			}}}`))
		case r.Method == http.MethodGet && r.URL.Path == "/api/terrapod/v1/agent-pools":
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"apool-aaa","type":"agent-pools","attributes":{"name":"aws-prod"}},
			  {"id":"apool-bbb","type":"agent-pools","attributes":{"name":"gcp-dev"}}
			]}`))
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/terrapod/v1/agent-pools/"):
			_, _ = w.Write([]byte(`{"data":{"id":"apool-aaa","type":"agent-pools","attributes":{"name":"aws-prod","labels":{"env":"prod"}}}}`))
		case r.Method == http.MethodPatch:
			_, _ = w.Write([]byte(`{"data":{"id":"apool-aaa","type":"agent-pools","attributes":{"name":"aws-prod","description":"renamed"}}}`))
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

func TestCreateAgentPool_LabelsAndOwner(t *testing.T) {
	c, lastBody := newPoolFixture(t)
	p, err := c.CreateAgentPool(t.Context(), CreateAgentPoolRequest{
		Name:        "aws-prod",
		Description: "AWS prod runners",
		Labels:      map[string]string{"region": "us-east-1", "env": "prod"},
		OwnerEmail:  "sre@example.com",
	})
	if err != nil {
		t.Fatalf("CreateAgentPool: %v", err)
	}
	if p.ID != "apool-aaa" || p.Name != "aws-prod" || p.Labels["env"] != "prod" {
		t.Errorf("pool: %+v", p)
	}
	var req struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	_ = json.Unmarshal(*lastBody, &req)
	if req.Data.Attributes["owner-email"] != "sre@example.com" {
		t.Errorf("owner-email missing/wrong: %+v", req.Data.Attributes)
	}
	if labels, ok := req.Data.Attributes["labels"].(map[string]any); !ok || labels["env"] != "prod" {
		t.Errorf("labels: %+v", req.Data.Attributes["labels"])
	}
}

func TestGetAgentPool(t *testing.T) {
	c, _ := newPoolFixture(t)
	p, err := c.GetAgentPool(t.Context(), "apool-aaa")
	if err != nil {
		t.Fatal(err)
	}
	if p.Labels["env"] != "prod" {
		t.Errorf("labels not parsed: %+v", p)
	}
}

func TestListAgentPools(t *testing.T) {
	c, _ := newPoolFixture(t)
	pools, err := c.ListAgentPools(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(pools) != 2 {
		t.Errorf("got %d pools", len(pools))
	}
}

func TestUpdateAgentPool_PointerSemantics(t *testing.T) {
	// nil Description ⇒ body omits it; &"" ⇒ explicitly clears.
	c, lastBody := newPoolFixture(t)
	desc := ""
	_, err := c.UpdateAgentPool(t.Context(), "apool-aaa", UpdateAgentPoolRequest{
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
	v, has := req.Data.Attributes["description"]
	if !has {
		t.Fatal("description should be present (explicit-clear semantics)")
	}
	if v != "" {
		t.Errorf("description should be empty, got: %v", v)
	}
}

func TestUpdateAgentPool_LeaveLabelsAlone(t *testing.T) {
	// Nil Labels in the SDK request ⇒ no labels key in body.
	c, lastBody := newPoolFixture(t)
	_, err := c.UpdateAgentPool(t.Context(), "apool-aaa", UpdateAgentPoolRequest{
		Name: "aws-prod",
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
	if _, has := req.Data.Attributes["labels"]; has {
		t.Errorf("labels leaked into PATCH body: %+v", req.Data.Attributes)
	}
}

func TestUpdateAgentPool_ClearLabels(t *testing.T) {
	// &{} explicitly clears labels.
	c, lastBody := newPoolFixture(t)
	empty := map[string]string{}
	_, err := c.UpdateAgentPool(t.Context(), "apool-aaa", UpdateAgentPoolRequest{
		Labels: &empty,
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
	if labels, has := req.Data.Attributes["labels"]; !has {
		t.Errorf("labels should be present (clear semantics): %+v", req.Data.Attributes)
	} else if m, ok := labels.(map[string]any); !ok || len(m) != 0 {
		t.Errorf("labels should be empty map, got: %v", labels)
	}
}

func TestDeleteAgentPool(t *testing.T) {
	c, _ := newPoolFixture(t)
	if err := c.DeleteAgentPool(t.Context(), "apool-aaa"); err != nil {
		t.Error(err)
	}
}
