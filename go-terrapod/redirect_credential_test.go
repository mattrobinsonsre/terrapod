package terrapod

import (
	"context"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
)

// GHSA-5fh8-vj57-6gvh, G1. The stdlib strips Authorization on a cross-DOMAIN
// redirect, which is why "302 to attacker.tld" is not the finding. Its rule is
// isDomainOrSubdomain on the hostname alone, so it ignores scheme and port —
// leaving a scheme downgrade and a sibling subdomain both carrying the token.
//
// Concretely reachable: GetRunPlanJSON documents that the endpoint 302s to a
// presigned storage URL and the client follows it, so a deployment whose object
// storage sits on a subdomain of the API host handed the platform token to the
// storage tier on every plan-JSON fetch.

func req(t *testing.T, raw string) *http.Request {
	t.Helper()
	u, err := url.Parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	r := &http.Request{URL: u, Header: http.Header{}}
	r.Header.Set("Authorization", "Bearer SECRET")
	return r
}

func TestRedirectDropsTheCredentialWhenItIsNotSafeToCarry(t *testing.T) {
	origin := req(t, "https://terrapod.example.com/api/v1/x")

	cases := []struct {
		name, target string
		keep         bool
	}{
		{"same host over https keeps it", "https://terrapod.example.com/other", true},
		{"scheme downgrade drops it", "http://terrapod.example.com/other", false},
		{"sibling subdomain drops it", "https://evil.terrapod.example.com/x", false},
		{"parent domain drops it", "https://example.com/x", false},
		{"presigned storage on another host drops it", "https://s3.amazonaws.com/bucket/k", false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			r := req(t, c.target)
			if err := dropCredentialOnUnsafeRedirect(r, []*http.Request{origin}); err != nil {
				t.Fatalf("redirect refused: %v", err)
			}
			got := r.Header.Get("Authorization") != ""
			if got != c.keep {
				t.Errorf("Authorization present = %v, want %v (target %s)", got, c.keep, c.target)
			}
		})
	}
}

func TestRedirectChainIsBounded(t *testing.T) {
	origin := req(t, "https://terrapod.example.com/x")
	via := make([]*http.Request, 5)
	for i := range via {
		via[i] = origin
	}
	if err := dropCredentialOnUnsafeRedirect(req(t, "https://terrapod.example.com/y"), via); err == nil {
		t.Error("an unbounded redirect chain was allowed")
	}
}

// End to end: the token must not reach a redirect target, and the body must
// still come back — the presigned-storage path depends on the redirect working.
func TestTheTokenDoesNotReachARedirectTarget(t *testing.T) {
	var reachedAuth string
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reachedAuth = r.Header.Get("Authorization")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":{"id":"x","type":"t","attributes":{}}}`))
	}))
	defer target.Close()

	origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL+"/presigned", http.StatusFound)
	}))
	defer origin.Close()

	c, err := NewClient(Options{BaseURL: origin.URL, Token: "SECRET"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.Get(context.Background(), "/api/v1/thing"); err != nil {
		t.Fatalf("the redirect was not followed: %v", err)
	}
	if reachedAuth != "" {
		t.Errorf("the redirect target received the credential: %q", reachedAuth)
	}
}

// The injected-client path had NO redirect protection: CheckRedirect was set
// only on the default client. The MCP server and the load-test harness both
// supply their own, so the component that runs unattended against production
// was precisely the one without the fix.
func TestAnInjectedHTTPClientStillGetsRedirectProtection(t *testing.T) {
	c, err := NewClient(Options{
		BaseURL:    "https://terrapod.example.com",
		Token:      "t",
		HTTPClient: &http.Client{},
	})
	if err != nil {
		t.Fatal(err)
	}
	if c.HTTPClient.CheckRedirect == nil {
		t.Fatal("an injected client was left with no redirect policy")
	}
}

func TestAnInjectedClientIsNotMutated(t *testing.T) {
	caller := &http.Client{}
	if _, err := NewClient(Options{
		BaseURL: "https://terrapod.example.com", Token: "t", HTTPClient: caller,
	}); err != nil {
		t.Fatal(err)
	}
	if caller.CheckRedirect != nil {
		t.Error("NewClient mutated the caller's http.Client")
	}
}

func TestRedirectToADifferentPortDropsTheCredential(t *testing.T) {
	// Comparing Hostname() alone ignored the port, so another service on the
	// same host — a registry, a preview app, a metrics UI — got the token.
	origin := req(t, "https://terrapod.example.com/api/v1/x")
	r := req(t, "https://terrapod.example.com:8443/x")
	if err := dropCredentialOnUnsafeRedirect(r, []*http.Request{origin}); err != nil {
		t.Fatal(err)
	}
	if r.Header.Get("Authorization") != "" {
		t.Error("a redirect to another port kept the credential")
	}
}

func TestAPlaintextLoopbackRedirectKeepsWorking(t *testing.T) {
	// The loopback carve-out permits http://127.0.0.1, so a bare
	// trailing-slash 301 from that server back to itself must not be stripped
	// — testing `scheme != "https"` would have 401'd a dev deployment that
	// worked on the previous release.
	origin := req(t, "http://127.0.0.1:8080/api/v1/x")
	r := req(t, "http://127.0.0.1:8080/api/v1/x/")
	if err := dropCredentialOnUnsafeRedirect(r, []*http.Request{origin}); err != nil {
		t.Fatal(err)
	}
	if r.Header.Get("Authorization") == "" {
		t.Error("a same-origin loopback redirect lost its credential")
	}
}
