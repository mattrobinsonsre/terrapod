package terrapod

import (
	"crypto/md5" //nolint:gosec
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newStateVersionFixture(t *testing.T) (*Client, *[]byte, *[]byte) {
	t.Helper()
	var createBody []byte
	var uploadBody []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/state-versions"):
			if b, err := io.ReadAll(r.Body); err == nil {
				createBody = b
			}
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"sv-aaa","type":"state-versions","attributes":{"serial":3,"lineage":"abc-123","md5":"x"}}}`))
		case r.Method == http.MethodPut && strings.Contains(r.URL.Path, "/state-versions/") && strings.HasSuffix(r.URL.Path, "/content"):
			if b, err := io.ReadAll(r.Body); err == nil {
				uploadBody = b
			}
			w.WriteHeader(http.StatusNoContent)
		case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/current-state-version"):
			_, _ = w.Write([]byte(`{"data":{"id":"sv-aaa","type":"state-versions","attributes":{"serial":3,"lineage":"abc-123"}}}`))
		default:
			http.Error(w, "unhandled "+r.URL.Path, http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c, &createBody, &uploadBody
}

func TestCreateStateVersion(t *testing.T) {
	c, createBody, _ := newStateVersionFixture(t)
	sv, err := c.CreateStateVersion(t.Context(), "ws-aaa", CreateStateVersionRequest{
		Serial:  3,
		Lineage: "abc-123",
		MD5:     "x",
	})
	if err != nil {
		t.Fatal(err)
	}
	if sv.ID != "sv-aaa" || sv.Serial != 3 {
		t.Errorf("state-version: %+v", sv)
	}
	var req struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	_ = json.Unmarshal(*createBody, &req)
	if req.Data.Attributes["serial"] == nil || req.Data.Attributes["lineage"] != "abc-123" {
		t.Errorf("body shape: %+v", req.Data.Attributes)
	}
}

func TestUploadStateContent_RawBytes(t *testing.T) {
	// The content endpoint accepts raw bytes — not JSON:API. The
	// test verifies the SDK doesn't wrap the payload.
	c, _, uploadBody := newStateVersionFixture(t)
	raw := []byte(`{"version":4,"serial":3,"lineage":"abc-123","outputs":{}}`)
	if err := c.UploadStateContent(t.Context(), "sv-aaa", raw); err != nil {
		t.Fatal(err)
	}
	if string(*uploadBody) != string(raw) {
		t.Errorf("upload body got wrapped: %s", *uploadBody)
	}
}

func TestCreateAndUploadState_ComputesMD5WhenEmpty(t *testing.T) {
	c, createBody, _ := newStateVersionFixture(t)
	raw := []byte(`{"version":4}`)
	_, err := c.CreateAndUploadState(t.Context(), "ws-aaa", raw, CreateStateVersionRequest{
		Serial:  1,
		Lineage: "lin",
	})
	if err != nil {
		t.Fatal(err)
	}
	sum := md5.Sum(raw) //nolint:gosec
	wantMD5 := hex.EncodeToString(sum[:])
	var req struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	_ = json.Unmarshal(*createBody, &req)
	if req.Data.Attributes["md5"] != wantMD5 {
		t.Errorf("md5 = %v, want %s", req.Data.Attributes["md5"], wantMD5)
	}
}

func TestGetCurrentStateVersion(t *testing.T) {
	c, _, _ := newStateVersionFixture(t)
	sv, err := c.GetCurrentStateVersion(t.Context(), "ws-aaa")
	if err != nil {
		t.Fatal(err)
	}
	if sv.Lineage != "abc-123" {
		t.Errorf("state-version: %+v", sv)
	}
}
