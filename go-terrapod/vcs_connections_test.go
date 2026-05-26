package terrapod

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newVCSConnFixture(t *testing.T) (*Client, *[]byte, *string) {
	t.Helper()
	var lastBody []byte
	var lastMethod string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		lastMethod = r.Method
		if r.Body != nil {
			b, _ := io.ReadAll(r.Body)
			lastBody = b
			_ = r.Body.Close()
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/terrapod/v1/vcs-connections":
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"vcs-aaa","type":"vcs-connections","attributes":{"name":"github-prod","provider":"github","status":"active","has-token":true,"github-app-id":12345}}}`))
		case r.Method == http.MethodGet && r.URL.Path == "/api/terrapod/v1/vcs-connections":
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"vcs-aaa","type":"vcs-connections","attributes":{"name":"github-prod","provider":"github","has-token":true}},
			  {"id":"vcs-bbb","type":"vcs-connections","attributes":{"name":"gitlab-internal","provider":"gitlab","has-token":true}}
			]}`))
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/terrapod/v1/vcs-connections/"):
			_, _ = w.Write([]byte(`{"data":{"id":"vcs-aaa","type":"vcs-connections","attributes":{"name":"github-prod","provider":"github","has-token":true}}}`))
		case r.Method == http.MethodPatch:
			_, _ = w.Write([]byte(`{"data":{"id":"vcs-aaa","type":"vcs-connections","attributes":{"name":"github-renamed","provider":"github","has-token":true}}}`))
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
	return c, &lastBody, &lastMethod
}

func TestCreateVCSConnection_Github(t *testing.T) {
	c, lastBody, _ := newVCSConnFixture(t)
	v, err := c.CreateVCSConnection(t.Context(), CreateVCSConnectionRequest{
		Name:                 "github-prod",
		Provider:             "github",
		GithubAppID:          12345,
		GithubInstallationID: 67890,
		PrivateKey:           "-----BEGIN RSA-----\nkey\n-----END RSA-----",
	})
	if err != nil {
		t.Fatalf("CreateVCSConnection: %v", err)
	}
	if v.ID != "vcs-aaa" || v.Name != "github-prod" || v.Provider != "github" || v.GithubAppID != 12345 {
		t.Errorf("vcs-connection: %+v", v)
	}
	// Request body shape — private-key sent, never echoed back in
	// the response (HasToken=true indicates server has it).
	var req struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	_ = json.Unmarshal(*lastBody, &req)
	if req.Data.Attributes["private-key"] == nil {
		t.Errorf("private-key missing from request: %+v", req.Data.Attributes)
	}
	if !v.HasToken {
		t.Error("HasToken should be true on response")
	}
}

func TestCreateVCSConnection_Gitlab(t *testing.T) {
	c, lastBody, _ := newVCSConnFixture(t)
	_, err := c.CreateVCSConnection(t.Context(), CreateVCSConnectionRequest{
		Name:      "gitlab-internal",
		Provider:  "gitlab",
		ServerURL: "https://gitlab.acme.example",
		Token:     "glpat-...",
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
	if req.Data.Attributes["token"] == nil {
		t.Error("token missing from request")
	}
	if req.Data.Attributes["server-url"] != "https://gitlab.acme.example" {
		t.Errorf("server-url: %+v", req.Data.Attributes)
	}
}

func TestGetVCSConnection(t *testing.T) {
	c, _, _ := newVCSConnFixture(t)
	v, err := c.GetVCSConnection(t.Context(), "vcs-aaa")
	if err != nil {
		t.Fatal(err)
	}
	if v.ID != "vcs-aaa" {
		t.Errorf("id: %q", v.ID)
	}
}

func TestListVCSConnections(t *testing.T) {
	c, _, _ := newVCSConnFixture(t)
	list, err := c.ListVCSConnections(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(list) != 2 || list[1].Provider != "gitlab" {
		t.Errorf("list: %+v", list)
	}
}

func TestUpdateVCSConnection_RotateCredentialOnlyWhenSet(t *testing.T) {
	// Vanilla PATCH that only renames should NOT include a private-key
	// in the body (that'd clear or rotate the existing one). The SDK
	// drops empty PrivateKey/Token from the request.
	c, lastBody, _ := newVCSConnFixture(t)
	_, err := c.UpdateVCSConnection(t.Context(), "vcs-aaa", UpdateVCSConnectionRequest{
		Name: "github-renamed",
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
	if req.Data.Attributes["name"] != "github-renamed" {
		t.Errorf("name not in body: %+v", req.Data.Attributes)
	}
	if _, has := req.Data.Attributes["private-key"]; has {
		t.Errorf("private-key leaked into rename-only request: %+v", req.Data.Attributes)
	}
	if _, has := req.Data.Attributes["token"]; has {
		t.Errorf("token leaked into rename-only request: %+v", req.Data.Attributes)
	}
}

func TestUpdateVCSConnection_RotateCredential(t *testing.T) {
	c, lastBody, _ := newVCSConnFixture(t)
	_, err := c.UpdateVCSConnection(t.Context(), "vcs-aaa", UpdateVCSConnectionRequest{
		PrivateKey: "new-key",
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
	if req.Data.Attributes["private-key"] != "new-key" {
		t.Errorf("private-key not in body: %+v", req.Data.Attributes)
	}
}

func TestDeleteVCSConnection(t *testing.T) {
	c, _, _ := newVCSConnFixture(t)
	if err := c.DeleteVCSConnection(t.Context(), "vcs-aaa"); err != nil {
		t.Error(err)
	}
}
