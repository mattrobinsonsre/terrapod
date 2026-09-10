package terrapod

import (
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
)

func TestListEngines(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/engines" {
			http.NotFound(w, r)
			return
		}
		_, _ = w.Write([]byte(`{"data":[{"type":"engines","id":"pulumi","attributes":{"name":"pulumi"}},{"type":"engines","id":"terraform","attributes":{"name":"terraform"}}]}`))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	got, err := c.ListEngines(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{"pulumi", "terraform"}; !reflect.DeepEqual(got, want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

func TestListEngines_Error(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.ListEngines(t.Context()); err == nil {
		t.Error("want an error from a 500")
	}
}
