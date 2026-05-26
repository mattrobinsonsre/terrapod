package terrapod

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newRegModFixture(t *testing.T) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Body != nil {
			_, _ = io.ReadAll(r.Body)
			_ = r.Body.Close()
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/terrapod/v1/registry-modules":
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"mod-aaa","type":"registry-modules","attributes":{
			  "name":"vpc","provider":"aws","namespace":"default","status":"active",
			  "labels":{"team":"sre"}
			}}}`))
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/terrapod/v1/registry-modules/private/default/"):
			_, _ = w.Write([]byte(`{"data":{"id":"mod-aaa","type":"registry-modules","attributes":{
			  "name":"vpc","provider":"aws","namespace":"default"
			}}}`))
		case r.Method == http.MethodPatch:
			_, _ = w.Write([]byte(`{"data":{"id":"mod-aaa","type":"registry-modules","attributes":{
			  "name":"vpc","provider":"aws","vcs-repo-url":"https://github.com/org/repo"
			}}}`))
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

func TestCreateRegistryModule(t *testing.T) {
	c := newRegModFixture(t)
	m, err := c.CreateRegistryModule(t.Context(), CreateRegistryModuleRequest{
		Name:         "vpc",
		ProviderName: "aws",
		Labels:       map[string]string{"team": "sre"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if m.ID != "mod-aaa" || m.Labels["team"] != "sre" {
		t.Errorf("module: %+v", m)
	}
}

func TestGetRegistryModule(t *testing.T) {
	c := newRegModFixture(t)
	m, err := c.GetRegistryModule(t.Context(), "vpc", "aws")
	if err != nil {
		t.Fatal(err)
	}
	if m.Name != "vpc" {
		t.Errorf("module: %+v", m)
	}
}

func TestUpdateRegistryModule(t *testing.T) {
	c := newRegModFixture(t)
	repo := "https://github.com/org/repo"
	m, err := c.UpdateRegistryModule(t.Context(), "vpc", "aws", UpdateRegistryModuleRequest{
		VCSRepoURL: &repo,
	})
	if err != nil {
		t.Fatal(err)
	}
	if m.VCSRepoURL != repo {
		t.Errorf("module: %+v", m)
	}
}

func TestDeleteRegistryModule(t *testing.T) {
	c := newRegModFixture(t)
	if err := c.DeleteRegistryModule(t.Context(), "vpc", "aws"); err != nil {
		t.Error(err)
	}
}
