package mcpserver

import (
	"crypto/tls"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

// refreshTransport re-resolves the API token from the credentials file when a
// request comes back 401, then retries the request once with the fresh token
// (issue #880).
//
// Without this, terrapod-mcp reads the token once at startup and reuses it for
// the life of the process: when the token expires and the operator refreshes it
// with `tofu login <host>` (or edits the credentials file), the running server
// keeps sending the stale token and every tool call fails with "Invalid or
// expired token" until the MCP is reconnected — even though the file now holds
// a valid token. This transport makes an in-session refresh transparent.
//
// Only a file-sourced token is refreshed. An explicit --token / $TERRAPOD_TOKEN
// is static (refresh is nil), so a genuinely-bad explicit token surfaces its
// 401 immediately instead of looping.
type refreshTransport struct {
	base http.RoundTripper
	// refresh re-resolves the token (reads the credentials file for the bound
	// host). nil when the token is fixed (flag/env) and must not be re-read.
	refresh func() string

	// apiHost is the Terrapod API's host[:port]. The bearer token is ONLY sent
	// to this host. A go-terrapod call that 302-redirects to a presigned object
	// URL (S3/GCS/Azure/filesystem on a different host) must NOT carry the
	// Authorization header: the presigned query string IS the auth, and S3
	// rejects a request bearing two auth mechanisms (400 InvalidArgument —
	// issue #1077). Go's stdlib strips Authorization on a cross-domain redirect,
	// but this RoundTripper runs for the redirected request too, so it must not
	// re-add the header for a non-API host.
	apiHost string

	mu    sync.RWMutex
	token string // best-known current token (starts at the startup token)
}

func (t *refreshTransport) current() string {
	t.mu.RLock()
	defer t.mu.RUnlock()
	return t.token
}

func (t *refreshTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	// A request to any host OTHER than the Terrapod API is a redirect to a
	// self-authenticating presigned object URL (plan JSON / state / artifacts in
	// S3 etc.). It must go out with NO bearer token — sending one makes S3 reject
	// it (two auth mechanisms, issue #1077) — and there is nothing to refresh.
	// Actively STRIP any Authorization the caller or Go's redirect copier left on
	// (belt-and-suspenders), then pass it through. Clone first — a RoundTripper
	// must not mutate the caller's request.
	if t.apiHost != "" && req.URL.Host != t.apiHost {
		if req.Header.Get("Authorization") != "" {
			clean := req.Clone(req.Context())
			clean.Header.Del("Authorization")
			return t.base.RoundTrip(clean)
		}
		return t.base.RoundTrip(req)
	}

	// Send our best-known token. After a refresh this is fresher than the value
	// the go-terrapod client re-sets from its startup token on every request, so
	// subsequent calls succeed on the first attempt instead of 401→refresh→retry.
	// Clone first — a RoundTripper must not mutate the caller's request.
	first := req.Clone(req.Context())
	if cur := t.current(); cur != "" {
		first.Header.Set("Authorization", "Bearer "+cur)
	}
	resp, err := t.base.RoundTrip(first)
	if err != nil || resp.StatusCode != http.StatusUnauthorized || t.refresh == nil {
		return resp, err
	}

	fresh := t.refresh()
	if fresh == "" || fresh == t.current() {
		return resp, err // no newer token to try — surface the 401
	}

	// Build a replayable retry. A body is only safe to resend when net/http gave
	// us a GetBody (it does for the bytes.Reader bodies go-terrapod sends).
	retry := req.Clone(req.Context())
	if req.Body != nil {
		if req.GetBody == nil {
			return resp, err
		}
		body, gErr := req.GetBody()
		if gErr != nil {
			return resp, err
		}
		retry.Body = body
	}
	retry.Header.Set("Authorization", "Bearer "+fresh)

	_ = resp.Body.Close() // discard the 401 so the connection can be reused
	t.mu.Lock()
	t.token = fresh
	t.mu.Unlock()
	return t.base.RoundTrip(retry)
}

// newHTTPClient builds the http.Client go-terrapod uses, wrapping its transport
// with token-refresh-on-401 (issue #880). When refreshable is true the token is
// re-read from the credentials file for host on a 401; when false (explicit
// --token / $TERRAPOD_TOKEN) the token is pinned. skipTLSVerify mirrors what
// go-terrapod would do for a self-signed dev instance (go-terrapod ignores its
// own SkipTLSVerify once an HTTPClient is injected, so we set it here).
func newHTTPClient(host, token string, refreshable, skipTLSVerify bool) *http.Client {
	base := &http.Transport{
		TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS13}, //nolint:gosec
	}
	if skipTLSVerify {
		base.TLSClientConfig.InsecureSkipVerify = true //nolint:gosec
	}
	rt := &refreshTransport{base: base, token: token, apiHost: hostOf(host)}
	if refreshable {
		rt.refresh = func() string { return tokenFromCredentialsFile(host) }
	}
	return &http.Client{Transport: rt, Timeout: 30 * time.Second}
}

// hostOf extracts the host[:port] from a Terrapod base URL or bare hostname so
// the bearer token can be scoped to exactly that host (issue #1077). Returns ""
// when it can't be parsed, in which case the transport falls back to its prior
// behaviour (inject on every request) rather than silently dropping auth.
func hostOf(s string) string {
	s = strings.TrimSpace(s)
	if s == "" {
		return ""
	}
	if !strings.Contains(s, "://") {
		s = "https://" + s
	}
	u, err := url.Parse(s)
	if err != nil {
		return ""
	}
	return u.Host
}
