package terrapod

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newPoolTokenFixture(t *testing.T) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Body != nil {
			_, _ = io.ReadAll(r.Body)
			_ = r.Body.Close()
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/tokens"):
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"at-aaa","type":"authentication-tokens","attributes":{
			  "token":"raw-secret","description":"ci","use-count":0,"is-revoked":false
			}}}`))
		case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/tokens"):
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"at-aaa","type":"authentication-tokens","attributes":{"description":"ci","use-count":3}}
			]}`))
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

func TestCreateAgentPoolToken(t *testing.T) {
	c := newPoolTokenFixture(t)
	tok, err := c.CreateAgentPoolToken(t.Context(), "apool-aaa", CreateAgentPoolTokenRequest{
		Description: "ci",
		MaxUses:     10,
		ExpiresAt:   "2030-01-01T00:00:00Z",
	})
	if err != nil {
		t.Fatal(err)
	}
	if tok.Token != "raw-secret" {
		t.Errorf("raw token should be returned on create: %+v", tok)
	}
}

func TestListAgentPoolTokens(t *testing.T) {
	c := newPoolTokenFixture(t)
	list, err := c.ListAgentPoolTokens(t.Context(), "apool-aaa")
	if err != nil {
		t.Fatal(err)
	}
	if len(list) != 1 || list[0].UseCount != 3 {
		t.Errorf("list: %+v", list)
	}
}

func TestGetAgentPoolToken(t *testing.T) {
	c := newPoolTokenFixture(t)
	tok, err := c.GetAgentPoolToken(t.Context(), "apool-aaa", "at-aaa")
	if err != nil || tok == nil {
		t.Fatalf("got %v / %v", tok, err)
	}
	// Raw token should NOT be in list response — only on create.
	if tok.Token != "" {
		t.Errorf("raw token leaked into list/get: %+v", tok)
	}
}

func TestDeleteAgentPoolToken(t *testing.T) {
	c := newPoolTokenFixture(t)
	if err := c.DeleteAgentPoolToken(t.Context(), "apool-aaa", "at-aaa"); err != nil {
		t.Error(err)
	}
}
