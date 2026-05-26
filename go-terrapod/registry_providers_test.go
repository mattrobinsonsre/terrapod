package terrapod

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newRegProvFixture(t *testing.T) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Body != nil {
			_, _ = io.ReadAll(r.Body)
			_ = r.Body.Close()
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/terrapod/v1/registry-providers":
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"prov-aaa","type":"registry-providers","attributes":{"name":"myprov","namespace":"default"}}}`))
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/terrapod/v1/registry-providers/private/default/"):
			_, _ = w.Write([]byte(`{"data":{"id":"prov-aaa","type":"registry-providers","attributes":{"name":"myprov","namespace":"default"}}}`))
		case r.Method == http.MethodPatch:
			_, _ = w.Write([]byte(`{"data":{"id":"prov-aaa","type":"registry-providers","attributes":{"name":"myprov","labels":{"team":"sre"}}}}`))
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

func TestCreateRegistryProvider(t *testing.T) {
	c := newRegProvFixture(t)
	p, err := c.CreateRegistryProvider(t.Context(), CreateRegistryProviderRequest{
		Name: "myprov",
	})
	if err != nil {
		t.Fatal(err)
	}
	if p.ID != "prov-aaa" {
		t.Errorf("provider: %+v", p)
	}
}

func TestGetRegistryProvider(t *testing.T) {
	c := newRegProvFixture(t)
	p, err := c.GetRegistryProvider(t.Context(), "myprov")
	if err != nil {
		t.Fatal(err)
	}
	if p.Name != "myprov" {
		t.Errorf("provider: %+v", p)
	}
}

func TestUpdateRegistryProvider(t *testing.T) {
	c := newRegProvFixture(t)
	labels := map[string]string{"team": "sre"}
	p, err := c.UpdateRegistryProvider(t.Context(), "myprov", UpdateRegistryProviderRequest{
		Labels: &labels,
	})
	if err != nil {
		t.Fatal(err)
	}
	if p.Labels["team"] != "sre" {
		t.Errorf("provider: %+v", p)
	}
}

func TestDeleteRegistryProvider(t *testing.T) {
	c := newRegProvFixture(t)
	if err := c.DeleteRegistryProvider(t.Context(), "myprov"); err != nil {
		t.Error(err)
	}
}
