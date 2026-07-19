package terrapod

import (
	"errors"
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

func TestListRegistryModules(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/api/terrapod/v1/registry-modules" {
			http.Error(w, "unhandled", http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(`{"data":[
		  {"id":"mod-aaa","type":"registry-modules","attributes":{"name":"vpc","provider":"aws"}},
		  {"id":"mod-bbb","type":"registry-modules","attributes":{"name":"eks","provider":"aws"}}
		]}`))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	mods, err := c.ListRegistryModules(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(mods) != 2 {
		t.Fatalf("expected 2 modules, got %d", len(mods))
	}
	if mods[0].Name != "vpc" || mods[1].Name != "eks" {
		t.Errorf("modules: %+v", mods)
	}
}

func TestGetModuleInterface(t *testing.T) {
	const ifacePath = "/api/terrapod/v1/registry-modules/private/default/vpc/aws/1.2.3/interface"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "unhandled", http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		if r.URL.Path == ifacePath {
			_, _ = w.Write([]byte(`{"data":{"type":"module-interface","id":"modver-xyz","attributes":{
			  "version":"1.2.3",
			  "inputs":[{"name":"cidr","type":"string","required":true},{"name":"tags","type":"map"}],
			  "outputs":[{"name":"vpc_id"}]
			}}}`))
			return
		}
		// Any other version → 404 (extraction disabled or unknown version).
		http.Error(w, `{"errors":[{"status":"404","title":"Not Found"}]}`, http.StatusNotFound)
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}

	iface, err := c.GetModuleInterface(t.Context(), "vpc", "aws", "1.2.3")
	if err != nil {
		t.Fatal(err)
	}
	if iface.Version != "1.2.3" {
		t.Errorf("version: %q", iface.Version)
	}
	if len(iface.Inputs) != 2 || len(iface.Outputs) != 1 {
		t.Fatalf("inputs=%d outputs=%d", len(iface.Inputs), len(iface.Outputs))
	}
	if iface.Inputs[0]["name"] != "cidr" {
		t.Errorf("input[0] name: %v", iface.Inputs[0]["name"])
	}

	// A missing/extraction-disabled version surfaces as *NotFoundError.
	_, err = c.GetModuleInterface(t.Context(), "vpc", "aws", "9.9.9")
	var nf *NotFoundError
	if !errors.As(err, &nf) {
		t.Errorf("expected *NotFoundError, got %v", err)
	}
}
