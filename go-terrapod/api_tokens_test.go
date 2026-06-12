package terrapod

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newAPITokenFixture(t *testing.T) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var reqBody string
		if r.Body != nil {
			b, _ := io.ReadAll(r.Body)
			reqBody = string(b)
			_ = r.Body.Close()
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/actions/rotate"):
			_, _ = w.Write([]byte(`{"data":{"id":"at-svc","type":"authentication-tokens","attributes":{
			  "token":"rotated-secret","kind":"service_bound","bound-to":"dev"}}}`))
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/actions/revoke-all"):
			// echo a revoked count; assert the email made it into the body
			if !strings.Contains(reqBody, "leaver@example.com") {
				http.Error(w, "missing email", http.StatusBadRequest)
				return
			}
			_, _ = w.Write([]byte(`{"data":{"email":"leaver@example.com","revoked":4}}`))
		case r.Method == http.MethodPost && strings.Contains(r.URL.Path, "/users/"):
			// create — echo back kind + pinned-roles so the caller can assert
			if !strings.Contains(reqBody, "service_bound") || !strings.Contains(reqBody, "deployer") {
				http.Error(w, "bad create body", http.StatusBadRequest)
				return
			}
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"at-new","type":"authentication-tokens","attributes":{
			  "token":"raw-secret","kind":"service_bound","bound-to":"dev","created-by":"dev",
			  "pinned-roles":["deployer"]}}}`))
		case r.Method == http.MethodPatch:
			_, _ = w.Write([]byte(`{"data":{"id":"at-svc","type":"authentication-tokens","attributes":{
			  "kind":"service_detached","bound-to":null,"pinned-roles":["deployer"]}}}`))
		case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/expiring"):
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"at-exp","type":"authentication-tokens","attributes":{"kind":"service_bound","expires-at":"2030-01-01T00:00:00Z"}}
			]}`))
		case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/admin/authentication-tokens"):
			// assert the kind filter was forwarded
			if r.URL.Query().Get("kind") != "service_detached" {
				http.Error(w, "missing kind filter", http.StatusBadRequest)
				return
			}
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"at-det","type":"authentication-tokens","attributes":{"kind":"service_detached","bound-to":null}}
			]}`))
		case r.Method == http.MethodGet && strings.Contains(r.URL.Path, "/users/"):
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"at-own","type":"authentication-tokens","attributes":{"kind":"interactive","bound-to":"dev"}}
			]}`))
		case r.Method == http.MethodGet:
			_, _ = w.Write([]byte(`{"data":{"id":"at-svc","type":"authentication-tokens","attributes":{
			  "kind":"service_bound","bound-to":"dev","pinned-roles":["deployer"]}}}`))
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

func TestCreateAPIToken(t *testing.T) {
	c := newAPITokenFixture(t)
	tok, err := c.CreateAPIToken(t.Context(), "dev", CreateAPITokenRequest{
		Description: "ci",
		Kind:        "service_bound",
		PinnedRoles: []string{"deployer"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if tok.Token != "raw-secret" {
		t.Errorf("raw token should be returned on create: %+v", tok)
	}
	if tok.Kind != "service_bound" || len(tok.PinnedRoles) != 1 || tok.PinnedRoles[0] != "deployer" {
		t.Errorf("kind/pinned-roles not parsed: %+v", tok)
	}
}

func TestListUserAPITokens(t *testing.T) {
	c := newAPITokenFixture(t)
	list, err := c.ListUserAPITokens(t.Context(), "dev")
	if err != nil {
		t.Fatal(err)
	}
	if len(list) != 1 || list[0].Kind != "interactive" {
		t.Errorf("list: %+v", list)
	}
}

func TestListAllAPITokensKindFilter(t *testing.T) {
	c := newAPITokenFixture(t)
	list, err := c.ListAllAPITokens(t.Context(), "service_detached")
	if err != nil {
		t.Fatal(err)
	}
	if len(list) != 1 || list[0].Kind != "service_detached" || list[0].BoundTo != "" {
		t.Errorf("filtered list: %+v", list)
	}
}

func TestListExpiringAPITokens(t *testing.T) {
	c := newAPITokenFixture(t)
	list, err := c.ListExpiringAPITokens(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(list) != 1 || list[0].ExpiresAt == "" {
		t.Errorf("expiring: %+v", list)
	}
}

func TestRotateAPIToken(t *testing.T) {
	c := newAPITokenFixture(t)
	tok, err := c.RotateAPIToken(t.Context(), "at-svc")
	if err != nil {
		t.Fatal(err)
	}
	if tok.Token != "rotated-secret" {
		t.Errorf("rotate should return a fresh secret: %+v", tok)
	}
}

func TestRetagAPITokenToDetached(t *testing.T) {
	c := newAPITokenFixture(t)
	tok, err := c.RetagAPIToken(t.Context(), "at-svc", "service_detached", []string{"deployer"})
	if err != nil {
		t.Fatal(err)
	}
	if tok.Kind != "service_detached" || tok.BoundTo != "" {
		t.Errorf("retag to detached should unbind: %+v", tok)
	}
}

func TestRevokeAllAPITokensForUser(t *testing.T) {
	c := newAPITokenFixture(t)
	n, err := c.RevokeAllAPITokensForUser(t.Context(), "leaver@example.com")
	if err != nil {
		t.Fatal(err)
	}
	if n != 4 {
		t.Errorf("expected 4 revoked, got %d", n)
	}
}

func TestRevokeAPIToken(t *testing.T) {
	c := newAPITokenFixture(t)
	if err := c.RevokeAPIToken(t.Context(), "at-svc"); err != nil {
		t.Fatal(err)
	}
}
