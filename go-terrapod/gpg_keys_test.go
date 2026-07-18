package terrapod

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newGPGFixture(t *testing.T) (*Client, *[]byte) {
	t.Helper()
	var lastBody []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Body != nil {
			b, _ := io.ReadAll(r.Body)
			lastBody = b
			_ = r.Body.Close()
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/revoke"):
			// A malformed / mismatched revocation certificate → 422.
			body := string(lastBody)
			if !strings.Contains(body, "revocation-certificate") ||
				strings.Contains(body, "\"revocation-certificate\":\"bad\"") {
				http.Error(w, `{"errors":[{"status":"422","title":"Unprocessable Entity","detail":"invalid revocation certificate"}]}`, http.StatusUnprocessableEntity)
				return
			}
			_, _ = w.Write([]byte(`{"data":{"id":"gpg-aaa","type":"gpg-keys","attributes":{"key-id":"ABC123","namespace":"default"}}}`))
		case r.Method == http.MethodPost && r.URL.Path == "/api/terrapod/v1/gpg-keys":
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"gpg-aaa","type":"gpg-keys","attributes":{
			  "key-id":"ABC123","namespace":"default","source":"terrapod"
			}}}`))
		case r.Method == http.MethodGet && r.URL.Path == "/api/terrapod/v1/gpg-keys":
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"gpg-aaa","type":"gpg-keys","attributes":{"key-id":"ABC123"}}
			]}`))
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/terrapod/v1/gpg-keys/"):
			_, _ = w.Write([]byte(`{"data":{"id":"gpg-aaa","type":"gpg-keys","attributes":{"key-id":"ABC123","namespace":"default"}}}`))
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
	return c, &lastBody
}

func TestCreateGPGKey_DefaultsApplied(t *testing.T) {
	c, lastBody := newGPGFixture(t)
	k, err := c.CreateGPGKey(t.Context(), CreateGPGKeyRequest{
		ASCIIArmor: "-----BEGIN PGP PUBLIC KEY BLOCK-----\n...\n",
	})
	if err != nil {
		t.Fatal(err)
	}
	if k.KeyID != "ABC123" {
		t.Errorf("key: %+v", k)
	}
	// SDK should send defaults: namespace=default, source=terrapod.
	var req struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	_ = json.Unmarshal(*lastBody, &req)
	if req.Data.Attributes["namespace"] != "default" || req.Data.Attributes["source"] != "terrapod" {
		t.Errorf("defaults missing: %+v", req.Data.Attributes)
	}
}

func TestGetGPGKey(t *testing.T) {
	c, _ := newGPGFixture(t)
	k, err := c.GetGPGKey(t.Context(), "gpg-aaa")
	if err != nil {
		t.Fatal(err)
	}
	if k.Namespace != "default" {
		t.Errorf("key: %+v", k)
	}
}

func TestListGPGKeys(t *testing.T) {
	c, _ := newGPGFixture(t)
	list, err := c.ListGPGKeys(t.Context())
	if err != nil || len(list) != 1 {
		t.Errorf("list: %v / %v", list, err)
	}
}

func TestRevokeGPGKey(t *testing.T) {
	c, lastBody := newGPGFixture(t)
	k, err := c.RevokeGPGKey(t.Context(), "gpg-aaa",
		"-----BEGIN PGP PUBLIC KEY BLOCK-----\nrevocation\n-----END PGP PUBLIC KEY BLOCK-----")
	if err != nil {
		t.Fatal(err)
	}
	if k.ID != "gpg-aaa" || k.KeyID != "ABC123" {
		t.Errorf("key: %+v", k)
	}
	// The SDK must send the certificate under revocation-certificate.
	var req struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	_ = json.Unmarshal(*lastBody, &req)
	if _, has := req.Data.Attributes["revocation-certificate"]; !has {
		t.Errorf("revocation-certificate not sent: %+v", req.Data.Attributes)
	}
}

func TestRevokeGPGKeyInvalidCertificate(t *testing.T) {
	c, _ := newGPGFixture(t)
	_, err := c.RevokeGPGKey(t.Context(), "gpg-aaa", "bad")
	if err == nil {
		t.Fatal("expected validation error")
	}
	if _, ok := err.(*ValidationError); !ok {
		t.Errorf("expected *ValidationError, got %T: %v", err, err)
	}
}

func TestDeleteGPGKey(t *testing.T) {
	c, _ := newGPGFixture(t)
	if err := c.DeleteGPGKey(t.Context(), "gpg-aaa"); err != nil {
		t.Error(err)
	}
}
