package terrapod

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newPolicyFixture(t *testing.T) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/policies"):
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"pol-111","type":"policies","attributes":{
			  "name":"deny-public","description":"no public buckets","rego":"package x",
			  "created-at":"2026-06-01T00:00:00Z","updated-at":"2026-06-01T00:00:00Z"
			},"relationships":{"policy-set":{"data":{"id":"polset-aaa","type":"policy-sets"}}}}}`))
		case r.Method == http.MethodPatch && strings.HasPrefix(r.URL.Path, "/api/terrapod/v1/policies/"):
			_, _ = w.Write([]byte(`{"data":{"id":"pol-111","type":"policies","attributes":{
			  "name":"renamed","rego":"package y"
			},"relationships":{"policy-set":{"data":{"id":"polset-aaa","type":"policy-sets"}}}}}`))
		case r.Method == http.MethodDelete && strings.HasPrefix(r.URL.Path, "/api/terrapod/v1/policies/"):
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
	return c
}

func TestAddPolicy(t *testing.T) {
	c := newPolicyFixture(t)
	p, err := c.AddPolicy(t.Context(), "polset-aaa", AddPolicyRequest{
		Name: "deny-public", Description: "no public buckets", Rego: "package x",
	})
	if err != nil {
		t.Fatal(err)
	}
	if p.ID != "pol-111" {
		t.Errorf("id = %q, want pol-111", p.ID)
	}
	if p.PolicySetID != "polset-aaa" {
		t.Errorf("policy-set id = %q, want polset-aaa", p.PolicySetID)
	}
	if p.Name != "deny-public" || p.Rego != "package x" {
		t.Errorf("unexpected policy: %+v", p)
	}
}

func TestUpdatePolicy(t *testing.T) {
	c := newPolicyFixture(t)
	name := "renamed"
	rego := "package y"
	// Only the PATCH branch returns the "renamed" body, so a successful
	// parse proves the method + path were correct.
	p, err := c.UpdatePolicy(t.Context(), "pol-111", UpdatePolicyRequest{Name: &name, Rego: &rego})
	if err != nil {
		t.Fatal(err)
	}
	if p.Name != "renamed" {
		t.Errorf("name = %q, want renamed", p.Name)
	}
}

func TestDeletePolicy(t *testing.T) {
	c := newPolicyFixture(t)
	if err := c.DeletePolicy(t.Context(), "pol-111"); err != nil {
		t.Fatal(err)
	}
}
