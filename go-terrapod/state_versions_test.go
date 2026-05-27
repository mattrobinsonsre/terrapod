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

// TestUploadStateContent_ContentType pins the wire format for the
// content endpoint: raw state bytes, Content-Type
// application/octet-stream (NOT application/vnd.api+json). A
// regression here would silently break a future stricter server.
func TestUploadStateContent_ContentType(t *testing.T) {
	var gotContentType string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPut && strings.HasSuffix(r.URL.Path, "/content") {
			gotContentType = r.Header.Get("Content-Type")
			w.WriteHeader(http.StatusNoContent)
			return
		}
		http.Error(w, "unhandled", http.StatusNotFound)
	}))
	defer srv.Close()
	c, _ := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err := c.UploadStateContent(t.Context(), "sv-x", []byte("raw")); err != nil {
		t.Fatal(err)
	}
	if gotContentType != "application/octet-stream" {
		t.Errorf("Content-Type = %q, want application/octet-stream", gotContentType)
	}
}

// TestCreateAndUploadState_RollbackOnUploadFail verifies the round-1
// orphan-rollback behaviour: when the /content PUT fails, the SDK
// must DELETE the just-created state-version record so a retry at
// the same serial doesn't 409-collide.
func TestCreateAndUploadState_RollbackOnUploadFail(t *testing.T) {
	var sawDelete bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/state-versions"):
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"sv-xxx","type":"state-versions","attributes":{"serial":1,"lineage":"L"}}}`))
		case r.Method == http.MethodPut && strings.HasSuffix(r.URL.Path, "/content"):
			http.Error(w, `{"errors":[{"status":"500","detail":"boom"}]}`, http.StatusInternalServerError)
		case r.Method == http.MethodDelete &&
			strings.HasPrefix(r.URL.Path, "/api/terrapod/v1/state-versions/") &&
			strings.HasSuffix(r.URL.Path, "/manage"):
			sawDelete = true
			w.WriteHeader(http.StatusNoContent)
		default:
			http.Error(w, "unhandled "+r.URL.Path, http.StatusNotFound)
		}
	}))
	defer srv.Close()
	c, _ := NewClient(Options{BaseURL: srv.URL, Token: "t"})

	_, err := c.CreateAndUploadState(t.Context(), "ws-a", []byte("state"), CreateStateVersionRequest{Serial: 1, Lineage: "L"})
	if err == nil {
		t.Fatal("expected upload error")
	}
	if !sawDelete {
		t.Errorf("expected orphan rollback DELETE, never fired")
	}
}

// TestGetAgentPoolToken_NotFound pins the round-1 contract change —
// the lookup used to return (nil, nil) on missing-id; it must now
// return *NotFoundError to match every other Get* in this SDK.
func TestGetAgentPoolToken_NotFound(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(`{"data":[],"meta":{"pagination":{"current-page":1,"total-pages":1,"total-count":0,"page-size":20}}}`))
	}))
	defer srv.Close()
	c, _ := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	tok, err := c.GetAgentPoolToken(t.Context(), "ap-x", "nope")
	if !IsNotFound(err) {
		t.Errorf("expected NotFoundError, got err=%v tok=%+v", err, tok)
	}
}

// TestGetVarsetVariable_NotFound — same contract test for varset
// variables.
func TestGetVarsetVariable_NotFound(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(`{"data":[]}`))
	}))
	defer srv.Close()
	c, _ := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	v, err := c.GetVarsetVariable(t.Context(), "vs-x", "nope")
	if !IsNotFound(err) {
		t.Errorf("expected NotFoundError, got err=%v v=%+v", err, v)
	}
}

// TestCreateStateVersion_Conflict pins the 409 → *ConflictError
// classification path. The migrator's writer relies on
// errors.As(err, &ConflictError{}) to distinguish "serial already
// exists" from other failures; a regression in parseStateVersion
// swallowing typed errors would silently break that branch.
func TestCreateStateVersion_Conflict(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		http.Error(w, `{"errors":[{"status":"409","detail":"serial 7 already exists"}]}`, http.StatusConflict)
	}))
	defer srv.Close()
	c, _ := NewClient(Options{BaseURL: srv.URL, Token: "t"})

	_, err := c.CreateStateVersion(t.Context(), "ws-a", CreateStateVersionRequest{Serial: 7, Lineage: "L"})
	if err == nil {
		t.Fatal("expected error")
	}
	if !IsConflict(err) {
		t.Errorf("expected *ConflictError, got %T: %v", err, err)
	}
}

// TestGetVariableByKey pins the new per-key lookup helper added in
// round 3. Verifies both the happy path and the NotFoundError
// contract.
func TestGetVariableByKey(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(`{"data":[
            {"id":"var-1","type":"vars","attributes":{"key":"AWS_REGION","value":"eu-west-1","category":"env","sensitive":false}},
            {"id":"var-2","type":"vars","attributes":{"key":"AWS_PROFILE","value":"dev","category":"env","sensitive":false}}
        ]}`))
	}))
	defer srv.Close()
	c, _ := NewClient(Options{BaseURL: srv.URL, Token: "t"})

	got, err := c.GetVariableByKey(t.Context(), "ws-a", "AWS_REGION")
	if err != nil {
		t.Fatal(err)
	}
	if got.ID != "var-1" || got.Value != "eu-west-1" {
		t.Errorf("variable: %+v", got)
	}

	missing, err := c.GetVariableByKey(t.Context(), "ws-a", "NOPE")
	if !IsNotFound(err) {
		t.Errorf("expected NotFoundError, got %v (missing=%+v)", err, missing)
	}
}
