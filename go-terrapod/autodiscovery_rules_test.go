package terrapod

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newAutodiscFixture(t *testing.T) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Body != nil {
			_, _ = io.ReadAll(r.Body)
			_ = r.Body.Close()
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/terrapod/v1/autodiscovery-rules":
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"ad-aaa","type":"autodiscovery-rules","attributes":{
			  "name":"monorepo","pattern":"services/*","branch":"main","enabled":true,
			  "created-at":"2025-01-01T00:00:00Z"
			}}}`))
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/terrapod/v1/autodiscovery-rules/"):
			_, _ = w.Write([]byte(`{"data":{"id":"ad-aaa","type":"autodiscovery-rules","attributes":{"name":"monorepo","pattern":"services/*","enabled":true}}}`))
		case r.Method == http.MethodPatch:
			_, _ = w.Write([]byte(`{"data":{"id":"ad-aaa","type":"autodiscovery-rules","attributes":{"name":"monorepo","pattern":"services/*","enabled":false}}}`))
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
	return c
}

func TestCreateAutodiscoveryRule(t *testing.T) {
	c := newAutodiscFixture(t)
	r, err := c.CreateAutodiscoveryRule(t.Context(), map[string]any{
		"name":     "monorepo",
		"pattern":  "services/*",
		"branch":   "main",
		"repo-url": "https://github.com/org/repo",
	})
	if err != nil {
		t.Fatal(err)
	}
	if r.ID != "ad-aaa" {
		t.Errorf("rule: %+v", r)
	}
	if r.Attributes["pattern"] != "services/*" {
		t.Errorf("attrs: %+v", r.Attributes)
	}
	if r.CreatedAt == "" {
		t.Errorf("created-at not parsed: %+v", r)
	}
}

func TestGetAutodiscoveryRule(t *testing.T) {
	c := newAutodiscFixture(t)
	r, err := c.GetAutodiscoveryRule(t.Context(), "ad-aaa")
	if err != nil {
		t.Fatal(err)
	}
	if r.Attributes["enabled"] != true {
		t.Errorf("attrs: %+v", r.Attributes)
	}
}

func TestUpdateAutodiscoveryRule(t *testing.T) {
	c := newAutodiscFixture(t)
	r, err := c.UpdateAutodiscoveryRule(t.Context(), "ad-aaa", map[string]any{
		"enabled": false,
	})
	if err != nil {
		t.Fatal(err)
	}
	if r.Attributes["enabled"] != false {
		t.Errorf("attrs: %+v", r.Attributes)
	}
}

func TestDeleteAutodiscoveryRule(t *testing.T) {
	c := newAutodiscFixture(t)
	if err := c.DeleteAutodiscoveryRule(t.Context(), "ad-aaa"); err != nil {
		t.Error(err)
	}
}
