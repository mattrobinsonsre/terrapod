package version

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestCheck_ExactMatch(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != DiscoveryPath {
			t.Errorf("unexpected path %q", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"terrapod-version": "0.27.0", "modules.v1": "/v1/modules/"}`))
	}))
	defer srv.Close()

	if err := Check(context.Background(), srv.URL, "0.27.0"); err != nil {
		t.Fatalf("expected exact match to pass, got: %v", err)
	}
}

func TestCheck_Mismatch(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"terrapod-version": "0.26.0"}`))
	}))
	defer srv.Close()

	err := Check(context.Background(), srv.URL, "0.27.0")
	if err == nil {
		t.Fatal("expected mismatch error")
	}
	if !errors.Is(err, ErrMismatch) {
		t.Errorf("expected ErrMismatch, got: %v", err)
	}
	msg := err.Error()
	// Operator-facing message must name both versions and the release URL
	// so they can act without reading code. Don't tighten — these are the
	// exact substrings the GitHub Release URL pattern depends on.
	for _, want := range []string{"tool=0.27.0", "api=0.26.0", "v0.26.0"} {
		if !strings.Contains(msg, want) {
			t.Errorf("error message missing %q: %s", want, msg)
		}
	}
}

func TestCheck_FieldMissing(t *testing.T) {
	// A Terrapod older than v0.24 won't include the version field. We
	// must surface this as ErrUnreported (operator action: upgrade), not
	// as ErrMismatch (operator action: install a matching tool release).
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"modules.v1": "/v1/modules/"}`))
	}))
	defer srv.Close()

	err := Check(context.Background(), srv.URL, "0.27.0")
	if !errors.Is(err, ErrUnreported) {
		t.Errorf("expected ErrUnreported, got: %v", err)
	}
}

func TestCheck_FieldEmpty(t *testing.T) {
	// An empty string is treated the same as missing — same operator
	// action and otherwise we'd compare "" to "0.27.0" and report mismatch
	// against a bogus value.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"terrapod-version": ""}`))
	}))
	defer srv.Close()

	err := Check(context.Background(), srv.URL, "0.27.0")
	if !errors.Is(err, ErrUnreported) {
		t.Errorf("expected ErrUnreported, got: %v", err)
	}
}

func TestCheck_DevBuildIsHandledLikeMismatch(t *testing.T) {
	// `go run` from a checkout sets Version="dev" (or empty after explicit
	// stripping); refusing to compare against "" prevents accidental writes
	// from an unidentified tool. Mismatch class because the resolution is
	// the same as a real mismatch (use a tagged binary, or override).
	err := Check(context.Background(), "https://unreachable.example", "")
	if !errors.Is(err, ErrMismatch) {
		t.Errorf("expected ErrMismatch for empty tool version, got: %v", err)
	}
}

func TestCheck_HTTP5xx(t *testing.T) {
	// A misconfigured or down target should surface the status to the
	// operator with the URL — not bury it as a generic mismatch.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "upstream barf", http.StatusBadGateway)
	}))
	defer srv.Close()

	err := Check(context.Background(), srv.URL, "0.27.0")
	if err == nil {
		t.Fatal("expected error on 502")
	}
	if !strings.Contains(err.Error(), "502") {
		t.Errorf("error should name the status code, got: %v", err)
	}
}

func TestCheck_TrailingSlashOnTargetBase(t *testing.T) {
	// Operators paste base URLs both with and without trailing slash.
	// Joining the path naively double-slashes; reject the URL or strip.
	// We strip — it's the principle of least surprise.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "//") {
			t.Errorf("double-slash leaked into request: %s", r.URL.Path)
		}
		_, _ = w.Write([]byte(`{"terrapod-version": "0.27.0"}`))
	}))
	defer srv.Close()

	if err := Check(context.Background(), srv.URL+"/", "0.27.0"); err != nil {
		t.Fatalf("expected pass with trailing slash, got: %v", err)
	}
}
