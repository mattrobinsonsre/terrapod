package terrapod

import (
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
)

const eePath = "/api/v1/ansible/execution-environments"

func eeServer(t *testing.T, body string) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != eePath {
			http.NotFound(w, r)
			return
		}
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestListAnsibleExecutionEnvironments(t *testing.T) {
	c := eeServer(t, `{"data":[
		{"type":"ansible-execution-environments","id":"2.21","attributes":{
			"kind":"built-in","ansible-core-line":"2.21","label":null,
			"image":"ghcr.io/x/terrapod-ee:2.21"}},
		{"type":"ansible-execution-environments","id":"hardened","attributes":{
			"kind":"operator","ansible-core-line":null,"label":"hardened",
			"image":"registry.internal/ee:1"}}
	],"meta":{"listing":"complete","pagination":{"total-count":2}}}`)

	got, err := c.ListAnsibleExecutionEnvironments(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	want := &AnsibleExecutionEnvironments{
		Environments: []AnsibleExecutionEnvironment{
			{ID: "2.21", Kind: KindBuiltIn, AnsibleCoreLine: "2.21", Image: "ghcr.io/x/terrapod-ee:2.21"},
			{ID: "hardened", Kind: KindOperator, Label: "hardened", Image: "registry.internal/ee:1"},
		},
		Listing: ListingComplete,
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("got %+v, want %+v", got, want)
	}
}

// A null `label` or `ansible-core-line` must decode to "" rather than failing:
// the server sends null for whichever half does not apply to that kind.
func TestListAnsibleExecutionEnvironments_NullsDecodeToEmpty(t *testing.T) {
	c := eeServer(t, `{"data":[{"id":"2.12","attributes":{
		"kind":"built-in","ansible-core-line":"2.12","label":null,"image":"r/ee:2.12"}}],
		"meta":{"listing":"complete"}}`)
	got, err := c.ListAnsibleExecutionEnvironments(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if got.Environments[0].Label != "" {
		t.Errorf("a null label should decode to empty, got %q", got.Environments[0].Label)
	}
}

// The important one. An empty list with an UNKNOWN listing means nothing at all,
// so a caller must be able to tell it from a deployment that genuinely publishes
// none -- and the two differ only in `meta.listing`.
func TestListAnsibleExecutionEnvironments_EmptyButUnknown(t *testing.T) {
	c := eeServer(t, `{"data":[],"meta":{"listing":"unknown"}}`)
	got, err := c.ListAnsibleExecutionEnvironments(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(got.Environments) != 0 {
		t.Fatalf("want no environments, got %d", len(got.Environments))
	}
	if got.Listing != ListingUnknown {
		t.Errorf("want %q, got %q", ListingUnknown, got.Listing)
	}
}

func TestListAnsibleExecutionEnvironments_EmptyAndComplete(t *testing.T) {
	c := eeServer(t, `{"data":[],"meta":{"listing":"complete"}}`)
	got, err := c.ListAnsibleExecutionEnvironments(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if got.Listing != ListingComplete {
		t.Errorf("want %q, got %q", ListingComplete, got.Listing)
	}
}

// A server predating `meta.listing` cannot tell us, and the unsafe default is
// to assume the listing is authoritative: that would present an empty set as
// "nothing available" on a deployment whose registry was simply unreachable.
func TestListAnsibleExecutionEnvironments_AbsentListingIsUnknown(t *testing.T) {
	c := eeServer(t, `{"data":[],"meta":{"pagination":{"total-count":0}}}`)
	got, err := c.ListAnsibleExecutionEnvironments(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if got.Listing != ListingUnknown {
		t.Errorf("an absent listing must read as unknown, got %q", got.Listing)
	}
}

// Never nil: a caller ranging over the result should get zero iterations rather
// than having to nil-check first.
func TestListAnsibleExecutionEnvironments_NeverNilSlice(t *testing.T) {
	c := eeServer(t, `{"data":[],"meta":{"listing":"complete"}}`)
	got, err := c.ListAnsibleExecutionEnvironments(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if got.Environments == nil {
		t.Error("want an empty slice, got nil")
	}
}

func TestListAnsibleExecutionEnvironments_Error(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.ListAnsibleExecutionEnvironments(t.Context()); err == nil {
		t.Error("want an error from a 500")
	}
}

func TestListAnsibleExecutionEnvironments_MalformedBody(t *testing.T) {
	c := eeServer(t, `not json`)
	if _, err := c.ListAnsibleExecutionEnvironments(t.Context()); err == nil {
		t.Error("want a parse error")
	}
}
