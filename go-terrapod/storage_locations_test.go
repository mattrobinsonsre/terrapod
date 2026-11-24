package terrapod

import (
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
)

const storageLocationsPath = "/api/v1/storage/locations"

func locationsServer(t *testing.T, body string) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != storageLocationsPath {
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

func TestListStorageLocations(t *testing.T) {
	c := locationsServer(t, `{"data":[
		{"type":"storage-locations","id":"archive","attributes":{
			"name":"archive","backend":"s3"}},
		{"type":"storage-locations","id":"eu-restricted","attributes":{
			"name":"eu-restricted","backend":"azure"}}
	],"meta":{"pagination":{"total-count":2}}}`)

	got, err := c.ListStorageLocations(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	want := []StorageLocation{
		{Name: "archive", Backend: "s3"},
		{Name: "eu-restricted", Backend: "azure"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v want %#v", got, want)
	}
}

// A single-store deployment -- every deployment that has not configured
// `storage.locations`. The empty slice must be non-nil and the error nil, so a
// caller can tell "offer no choice" from "the call failed".
func TestListStorageLocations_NoneConfigured(t *testing.T) {
	c := locationsServer(t, `{"data":[],"meta":{"pagination":{"total-count":0}}}`)

	got, err := c.ListStorageLocations(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if got == nil {
		t.Fatal("want a non-nil empty slice, got nil")
	}
	if len(got) != 0 {
		t.Fatalf("want empty, got %#v", got)
	}
}

// The id is the name, so a server that stopped sending the attribute still
// yields usable values rather than a slice of blanks. Worth pinning because the
// fallback is invisible when both are present, which is every real response.
func TestListStorageLocations_FallsBackToTheID(t *testing.T) {
	c := locationsServer(t, `{"data":[
		{"type":"storage-locations","id":"archive","attributes":{"backend":"s3"}}
	]}`)

	got, err := c.ListStorageLocations(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0].Name != "archive" {
		t.Fatalf("want the id as the name, got %#v", got)
	}
}

func TestListStorageLocations_ErrorIsWrapped(t *testing.T) {
	c := locationsServer(t, `{"data":`)

	if _, err := c.ListStorageLocations(t.Context()); err == nil {
		t.Fatal("want an error on a truncated body")
	}
}
