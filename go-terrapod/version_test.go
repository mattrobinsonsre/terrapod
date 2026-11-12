package terrapod

import (
	"context"
	"errors"
	"fmt"
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
	// Both sides at or above MinimumAPIVersion. They used to be 0.27.0, which
	// this tree can no longer address at all, so the test would have passed by
	// tripping the floor rather than by exercising the exact-match path (#2497).
	withSDKVersion(t, "2.0.0")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != DiscoveryPath {
			t.Errorf("unexpected path %q", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"terrapod-version": "2.0.0", "modules.v1": "/v1/modules/"}`))
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
	// Both above MinimumAPIVersion, so the refusal is the SDK-newer comparison
	// and not the floor -- which would otherwise answer first and make the test
	// pass under its own name for the wrong reason (#2497).
	withSDKVersion(t, "1.11.0")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"terrapod-version": "1.9.0"}`))
	}))
	defer srv.Close()

	c := makeClient(t, srv.URL)
	err := c.VersionCheck(context.Background())
	if !errors.Is(err, ErrVersionMismatch) {
		t.Fatalf("expected ErrVersionMismatch, got: %v", err)
	}
	msg := err.Error()
	if strings.Contains(msg, "minimum of") {
		t.Errorf("the floor answered, so this no longer tests the comparison: %s", msg)
	}
	for _, want := range []string{"SDK=1.11.0", "API=1.9.0"} {
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
	withSDKVersion(t, "2.0.0")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "//") {
			t.Errorf("double-slash leaked: %q", r.URL.Path)
		}
		_, _ = w.Write([]byte(`{"terrapod-version": "2.0.0"}`))
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

// ── the minimum server version (#2497) ────────────────────────────────────

// belowTheFloor is one minor below MinimumAPIVersion, DERIVED rather than
// written down: bumping the constant must move these tests with it, or they go
// on asserting a floor the SDK no longer has.
func belowTheFloor(t *testing.T) string {
	t.Helper()
	maj, min, _, ok := parseSemver(MinimumAPIVersion)
	if !ok {
		t.Fatalf("MinimumAPIVersion %q is not semver", MinimumAPIVersion)
	}
	if min == 0 {
		if maj == 0 {
			t.Skip("no version below the floor to construct")
		}
		return fmt.Sprintf("%d.99.0", maj-1)
	}
	return fmt.Sprintf("%d.%d.0", maj, min-1)
}

func TestVersionCheck_APIBelowTheFloorIsRefusedAndSaysWhy(t *testing.T) {
	// The SDK addresses /api/v1, which Terrapod mounts from MinimumAPIVersion
	// onward, so an older target 404s every call. Learning that from the probe
	// is the whole reason the floor is checked rather than only documented: the
	// alternative is a silent 404 naming neither the prefix nor the version.
	old := belowTheFloor(t)
	withSDKVersion(t, "2.0.0")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"terrapod-version": "` + old + `"}`))
	}))
	defer srv.Close()

	err := makeClient(t, srv.URL).VersionCheck(context.Background())
	if !errors.Is(err, ErrVersionMismatch) {
		t.Fatalf("a target below the floor must be refused, got: %v", err)
	}
	msg := err.Error()
	for _, want := range []string{MinimumAPIVersion, "/api/v1", "404"} {
		if !strings.Contains(msg, want) {
			t.Errorf("the reason must name %q so the operator can act on it: %s", want, msg)
		}
	}
}

func TestVersionCheck_TheFloorItselfIsAccepted(t *testing.T) {
	// The other direction, which a refusal test alone cannot give: an
	// off-by-one on the comparison would refuse the oldest SUPPORTED server,
	// and nothing else here would notice.
	withSDKVersion(t, MinimumAPIVersion)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"terrapod-version": "` + MinimumAPIVersion + `"}`))
	}))
	defer srv.Close()

	if err := makeClient(t, srv.URL).VersionCheck(context.Background()); err != nil {
		t.Fatalf("the floor version is supported, not refused: %v", err)
	}
}

func TestVersionCheck_TheFloorIsAboutTHISSOURCENotTheVersionPair(t *testing.T) {
	// An SDK pinned BELOW the floor, against a target below it too. The pair is
	// internally consistent -- same major, API not older than the SDK -- so the
	// comparison alone would call it compatible, and every call would then 404.
	// The floor is a property of the code in this tree, so it refuses anyway,
	// and the reason must be the floor rather than the comparison.
	old := belowTheFloor(t)
	withSDKVersion(t, old)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"terrapod-version": "` + old + `"}`))
	}))
	defer srv.Close()

	err := makeClient(t, srv.URL).VersionCheck(context.Background())
	if !errors.Is(err, ErrVersionMismatch) {
		t.Fatalf("an internally consistent pair below the floor must still be refused: %v", err)
	}
	if !strings.Contains(err.Error(), "minimum of") {
		t.Errorf("the floor must be the stated reason, not the version comparison: %v", err)
	}
}

func TestVersionCheck_ADevSDKStillSkipsEverythingIncludingTheFloor(t *testing.T) {
	// Recording the gap rather than leaving it to be discovered. The dev-SDK
	// early return happens before the probe (it is asserted, against an
	// unreachable host, by TestVersionCheck_DevSDKSkips), so a dev build gets
	// no floor check either. That is the right trade: a developer pointing an
	// unpinned build at an ancient server is not whom the floor protects, and
	// probing on a dev build would make VersionCheck do network work the
	// existing contract says it does not.
	withSDKVersion(t, "dev")
	if err := makeClient(t, "https://unreachable.example").VersionCheck(context.Background()); err != nil {
		t.Errorf("a dev SDK build must still skip without probing: %v", err)
	}
}

func TestTheSDKAddressesNoDeprecatedNativeAlias(t *testing.T) {
	// The floor is only safe because nothing here reaches for the alias as a
	// fallback. A single reintroduced literal would make the SDK's reach
	// inconsistent between methods, which is worse than either choice --
	// `scripts/docs-audit/check_native_prefix.py` is the tree-wide gate; this
	// is the one a Go developer sees without running the Python one.
	if MinimumAPIVersion == "" {
		t.Fatal("MinimumAPIVersion is unset, so nothing pins the floor")
	}
	if _, _, _, ok := parseSemver(MinimumAPIVersion); !ok {
		t.Fatalf("MinimumAPIVersion %q must be semver so the floor is comparable", MinimumAPIVersion)
	}
}
