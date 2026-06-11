package terrapod

import (
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

// captures the last request the publish helpers made.
type capturedReq struct {
	method string
	path   string
	body   []byte
	ctype  string
}

func newPublishFixture(t *testing.T, status int) (*Client, *capturedReq) {
	t.Helper()
	cap := &capturedReq{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		cap.method = r.Method
		cap.path = r.URL.Path
		cap.ctype = r.Header.Get("Content-Type")
		if r.Body != nil {
			cap.body, _ = io.ReadAll(r.Body)
			_ = r.Body.Close()
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		if status >= 400 {
			w.WriteHeader(status)
			_, _ = w.Write([]byte(`{"errors":[{"detail":"signature failed verification"}]}`))
			return
		}
		w.WriteHeader(status)
		_, _ = w.Write([]byte(`{"data":{"id":"v","type":"registry-provider-versions","attributes":{}}}`))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c, cap
}

func TestUploadProviderSHASUMS(t *testing.T) {
	c, cap := newPublishFixture(t, http.StatusOK)
	if err := c.UploadProviderSHASUMS(t.Context(), "awsmai", "1.0.0", []byte("sums")); err != nil {
		t.Fatal(err)
	}
	if cap.method != http.MethodPut {
		t.Errorf("method = %s", cap.method)
	}
	if cap.path != "/api/terrapod/v1/registry-providers/private/default/awsmai/versions/1.0.0/shasums" {
		t.Errorf("path = %s", cap.path)
	}
	if string(cap.body) != "sums" {
		t.Errorf("body wrapped/altered: %q", cap.body)
	}
}

func TestUploadProviderSignaturePath(t *testing.T) {
	c, cap := newPublishFixture(t, http.StatusOK)
	if err := c.UploadProviderSignature(t.Context(), "awsmai", "1.0.0", []byte("sig")); err != nil {
		t.Fatal(err)
	}
	if cap.path != "/api/terrapod/v1/registry-providers/private/default/awsmai/versions/1.0.0/shasums.sig" {
		t.Errorf("path = %s", cap.path)
	}
}

func TestUploadProviderSignatureRejected(t *testing.T) {
	c, _ := newPublishFixture(t, http.StatusUnprocessableEntity)
	err := c.UploadProviderSignature(t.Context(), "awsmai", "1.0.0", []byte("badsig"))
	if !IsValidation(err) {
		t.Fatalf("expected ValidationError, got %v", err)
	}
}

func TestUploadProviderPlatform(t *testing.T) {
	c, cap := newPublishFixture(t, http.StatusOK)
	zip := []byte("PK\x03\x04zip")
	if err := c.UploadProviderPlatform(t.Context(), "awsmai", "1.0.0", "linux", "arm64", zip); err != nil {
		t.Fatal(err)
	}
	if cap.path != "/api/terrapod/v1/registry-providers/private/default/awsmai/versions/1.0.0/platforms/linux/arm64" {
		t.Errorf("path = %s", cap.path)
	}
	if string(cap.body) != string(zip) {
		t.Errorf("zip body altered")
	}
}

func TestUploadProviderPlatformMismatchRejected(t *testing.T) {
	c, _ := newPublishFixture(t, http.StatusUnprocessableEntity)
	err := c.UploadProviderPlatform(t.Context(), "awsmai", "1.0.0", "linux", "arm64", []byte("x"))
	if !IsValidation(err) {
		t.Fatalf("expected ValidationError, got %v", err)
	}
}

func TestUploadModuleVersion(t *testing.T) {
	c, cap := newPublishFixture(t, http.StatusOK)
	if err := c.UploadModuleVersion(t.Context(), "vpc", "aws", "1.2.3", []byte("targz")); err != nil {
		t.Fatal(err)
	}
	if cap.path != "/api/terrapod/v1/registry-modules/private/default/vpc/aws/versions/1.2.3/upload" {
		t.Errorf("path = %s", cap.path)
	}
	if string(cap.body) != "targz" {
		t.Errorf("tarball body altered")
	}
}
