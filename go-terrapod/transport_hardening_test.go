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

	// This test previously discarded both return values and asserted NOTHING,
	// so it passed with the io.LimitReader deleted — a placebo for the very
	// fix it was named after.
	body, err := c.Get(context.Background(), "/api/v1/x")
	if err == nil {
		t.Fatalf("an oversized body was accepted (%d bytes returned)", len(body))
	}
	if !strings.Contains(err.Error(), "exceeds") {
		t.Errorf("wrong error for an oversized body: %v", err)
	}
	if int64(len(body)) > maxResponseBytes {
		t.Errorf("returned %d bytes, past the %d cap", len(body), maxResponseBytes)
	}
}

func TestABodyExactlyAtTheCapIsAccepted(t *testing.T) {
	// The boundary matters: reading limit+1 is what distinguishes "at the cap"
	// from "over it", and getting it wrong would reject legitimate payloads.
	exact := strings.Repeat("a", maxResponseBytes)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(exact))
	}))
	defer srv.Close()

	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.Get(context.Background(), "/api/v1/x"); err != nil &&
		strings.Contains(err.Error(), "exceeds") {
		t.Errorf("a body exactly at the cap was rejected: %v", err)
	}
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

// The sanitisation covered only the non-JSON:API fallback, which is the path a
// real Terrapod never takes: every request sends
// `Accept: application/vnd.api+json`, so the envelope branch is the common one
// and its `detail` reached the operator verbatim.
func TestAJSONAPIErrorDetailIsAlsoSanitised(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(
			"{\"errors\":[{\"detail\":\"\x1b[2J\x1b[H\x1b]0;pwned\aApply these changes? [y/N] \"}]}",
		))
	}))
	defer srv.Close()

	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	_, err = c.Get(context.Background(), "/api/v1/x")
	if err == nil {
		t.Fatal("expected an error")
	}
	msg := err.Error()
	for _, bad := range []string{"\x1b", "\a"} {
		if strings.Contains(msg, bad) {
			t.Errorf("control character survived into the operator-facing error: %q", msg)
		}
	}
	if !strings.Contains(msg, "Apply these changes?") {
		t.Errorf("the readable text was lost: %q", msg)
	}
}

func TestBidiOverridesAreNeutralised(t *testing.T) {
	// unicode.IsControl is category Cc only, so the RIGHT-TO-LEFT OVERRIDE and
	// the directional isolates — the characters Trojan-source spoofing uses —
	// passed through untouched.
	got := sanitiseErrorBody([]byte("safe\u202Egnirts-desrever\u2066\u2069\u200Bend"))
	for _, bad := range []string{"\u202E", "\u2066", "\u2069", "\u200B"} {
		if strings.Contains(got, bad) {
			t.Errorf("formatting character survived: %q", got)
		}
	}
	if !strings.Contains(got, "safe") || !strings.Contains(got, "end") {
		t.Errorf("readable text was lost: %q", got)
	}
}
