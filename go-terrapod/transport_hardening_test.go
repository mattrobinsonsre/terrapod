package terrapod

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// GHSA-5fh8-vj57-6gvh, G3/G4/G5/G7.

func TestAPathParameterCannotInjectQueryOrTraversal(t *testing.T) {
	var seen, rawQuery string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r.URL.RequestURI()
		rawQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":{"id":"x","type":"t","attributes":{}}}`))
	}))
	defer srv.Close()

	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}

	// Observed on the wire before the fix: the id was concatenated raw, so a
	// "?" in it appended arbitrary query parameters to an authenticated request.
	_, _ = c.GetDeletedWorkspace(context.Background(), "ws-1?force=true&admin=1")

	// The property is that the id stays INSIDE the path segment: the "?" is
	// percent-encoded, so the server parses no query string at all. Asserting
	// on the substring "force=true" would be wrong — it legitimately appears
	// as inert escaped text within the path.
	if rawQuery != "" {
		t.Errorf("an id injected a query string: %q (uri %s)", rawQuery, seen)
	}
	if !strings.Contains(seen, "%3F") {
		t.Errorf("the '?' was not escaped, so it was not contained: %s", seen)
	}

	// Dot segments must not be able to walk to a different endpoint either.
	_, _ = c.GetDeletedWorkspace(context.Background(), "../../admin/users")
	if strings.Contains(seen, "admin/users") {
		t.Errorf("an id traversed to another endpoint: %s", seen)
	}
}

func TestALowLevelPathMustBeRooted(t *testing.T) {
	// "http://host" + "@evil:9/loot" parses with Host=evil:9 and the real host
	// demoted to userinfo — the bearer token goes to the attacker. Get and its
	// siblings are exported and documented for third-party automation, so the
	// path is not always ours.
	c, err := NewClient(Options{BaseURL: "https://terrapod.example.com", Token: "SECRET"})
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range []string{"@127.0.0.1:9/loot", "evil.example.com/x", ""} {
		if _, err := c.Get(context.Background(), p); err == nil {
			t.Errorf("an unrooted path was accepted: %q", p)
		} else if !strings.Contains(err.Error(), "must start with") {
			t.Errorf("unexpected error for %q: %v", p, err)
		}
	}
}

func TestAHostileResponseBodyIsBounded(t *testing.T) {
	huge := strings.Repeat("a", maxResponseBytes+4096)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(huge))
	}))
	defer srv.Close()

	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	// It will fail to decode as JSON:API — the point is that it was bounded on
	// the way in rather than read into memory without limit.
	_, _ = c.Get(context.Background(), "/api/v1/x")
}

func TestErrorBodiesCannotDriveTheTerminal(t *testing.T) {
	// These strings are intended for operator display, so a hostile server
	// could clear the screen, retitle the window and forge a prompt inside a
	// `terraform apply` transcript.
	got := sanitiseErrorBody([]byte("\x1b[2J\x1b[H\x1b]0;pwned\aAre you sure? [y/N] "))
	if strings.ContainsRune(got, '\x1b') || strings.ContainsRune(got, '\a') {
		t.Errorf("control characters survived: %q", got)
	}
	if !strings.Contains(got, "Are you sure?") {
		t.Errorf("the readable text was lost: %q", got)
	}

	long := sanitiseErrorBody([]byte(strings.Repeat("x", maxErrorBodyBytes*4)))
	if len(long) > maxErrorBodyBytes {
		t.Errorf("error body not truncated: %d bytes", len(long))
	}
}
