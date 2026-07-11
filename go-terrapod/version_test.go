package terrapod

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// withSDKVersion temporarily sets the package-level SDKVersion (which
// the release pipeline overrides via -ldflags in production) for a
// single test. Restores on cleanup so tests don't leak state.
func withSDKVersion(t *testing.T, version string) {
	t.Helper()
	prev := SDKVersion
	SDKVersion = version
	t.Cleanup(func() { SDKVersion = prev })
}

func makeClient(t *testing.T, baseURL string) *Client {
	t.Helper()
	c, err := NewClient(Options{BaseURL: baseURL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestVersionCheck_ExactMatch(t *testing.T) {
	withSDKVersion(t, "0.27.0")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != DiscoveryPath {
			t.Errorf("unexpected path %q", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"terrapod-version": "0.27.0", "modules.v1": "/v1/modules/"}`))
	}))
	defer srv.Close()

	c := makeClient(t, srv.URL)
	if err := c.VersionCheck(context.Background()); err != nil {
		t.Fatalf("expected nil, got: %v", err)
	}
}

func TestVersionCheck_APIOlderIsMismatch(t *testing.T) {
	// Same major, but the API is OLDER than the SDK was built against —
	// the SDK may call endpoints the older API lacks. Mismatch.
	withSDKVersion(t, "1.4.0")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"terrapod-version": "1.2.0"}`))
	}))
	defer srv.Close()

	c := makeClient(t, srv.URL)
	err := c.VersionCheck(context.Background())
	if !errors.Is(err, ErrVersionMismatch) {
		t.Fatalf("expected ErrVersionMismatch, got: %v", err)
	}
	msg := err.Error()
	for _, want := range []string{"SDK=1.4.0", "API=1.2.0"} {
		if !strings.Contains(msg, want) {
			t.Errorf("error message missing %q: %s", want, msg)
		}
	}
}

func TestVersionCheck_NewerAPISameMajorIsOK(t *testing.T) {
	// The API is NEWER than the SDK, same major → forward-compatible → nil.
	// This is the core case the old exact-match logic wrongly failed.
	withSDKVersion(t, "1.0.0")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"terrapod-version": "1.7.3"}`))
	}))
	defer srv.Close()

	c := makeClient(t, srv.URL)
	if err := c.VersionCheck(context.Background()); err != nil {
		t.Fatalf("newer API within same major must be compatible, got: %v", err)
	}
}

func TestVersionCheck_DifferentMajorIsMismatch(t *testing.T) {
	withSDKVersion(t, "1.9.0")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"terrapod-version": "2.0.0"}`))
	}))
	defer srv.Close()

	c := makeClient(t, srv.URL)
	err := c.VersionCheck(context.Background())
	if !errors.Is(err, ErrVersionMismatch) {
		t.Fatalf("expected ErrVersionMismatch across majors, got: %v", err)
	}
	if !strings.Contains(err.Error(), "major") {
		t.Errorf("error should name the major mismatch: %v", err)
	}
}

func TestVersionCheck_FieldMissingIsUnreported(t *testing.T) {
	// A Terrapod older than v0.24 won't include the version field.
	// We want ErrVersionUnreported (operator: upgrade target), distinct
	// from ErrVersionMismatch (operator: install matching SDK).
	withSDKVersion(t, "0.27.0")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"modules.v1": "/v1/modules/"}`))
	}))
	defer srv.Close()
	c := makeClient(t, srv.URL)
	err := c.VersionCheck(context.Background())
	if !errors.Is(err, ErrVersionUnreported) {
		t.Errorf("expected ErrVersionUnreported, got: %v", err)
	}
}

func TestVersionCheck_EmptyFieldIsUnreported(t *testing.T) {
	withSDKVersion(t, "0.27.0")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"terrapod-version": ""}`))
	}))
	defer srv.Close()
	c := makeClient(t, srv.URL)
	err := c.VersionCheck(context.Background())
	if !errors.Is(err, ErrVersionUnreported) {
		t.Errorf("expected ErrVersionUnreported, got: %v", err)
	}
}

func TestVersionCheck_DevSDKSkips(t *testing.T) {
	// SDKVersion="dev" → no pinned version to compare; skip silently
	// (return nil) rather than false-alarm. Must not even probe the server.
	withSDKVersion(t, "dev")
	c := makeClient(t, "https://unreachable.example")
	if err := c.VersionCheck(context.Background()); err != nil {
		t.Errorf("dev SDK build should skip the check (nil), got: %v", err)
	}
}

func TestVersionCheck_DevAPISkips(t *testing.T) {
	// The target reports "dev" → no meaningful version; skip → nil.
	withSDKVersion(t, "1.0.0")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"terrapod-version": "dev"}`))
	}))
	defer srv.Close()
	c := makeClient(t, srv.URL)
	if err := c.VersionCheck(context.Background()); err != nil {
		t.Errorf("dev API build should skip the check (nil), got: %v", err)
	}
}

func TestVersionCheck_HTTP5xx(t *testing.T) {
	// A misconfigured or down target should surface the status + URL
	// to operators — not bury as a generic mismatch.
	withSDKVersion(t, "0.27.0")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "upstream barf", http.StatusBadGateway)
	}))
	defer srv.Close()
	c := makeClient(t, srv.URL)
	err := c.VersionCheck(context.Background())
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(err.Error(), "502") {
		t.Errorf("error should name status, got: %v", err)
	}
}

func TestVersionCheck_TrailingSlashOnBaseURL(t *testing.T) {
	// Operator pastes "https://terrapod.example.com/" — strip via the
	// normaliser, don't double-slash the discovery path.
	withSDKVersion(t, "0.27.0")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "//") {
			t.Errorf("double-slash leaked: %q", r.URL.Path)
		}
		_, _ = w.Write([]byte(`{"terrapod-version": "0.27.0"}`))
	}))
	defer srv.Close()
	c, err := NewClient(Options{BaseURL: srv.URL + "/", Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	if err := c.VersionCheck(context.Background()); err != nil {
		t.Errorf("trailing slash should pass: %v", err)
	}
}
