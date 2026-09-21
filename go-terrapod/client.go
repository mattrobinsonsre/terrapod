package terrapod

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
	"unicode"
)

// Client is a Terrapod API client. Construct via NewClient. All resource
// methods (CreateWorkspace, ListWorkspaces, etc.) hang off this type.
//
// A Client is safe for concurrent use by multiple goroutines.
type Client struct {
	// BaseURL is the Terrapod base URL (scheme + host, no trailing slash).
	BaseURL string

	// Token is the Bearer token sent with every request. Sourced from
	// the operator (CLI flag, env var, ~/.terraform.d/credentials.tfrc.json).
	Token string

	// HTTPClient is the underlying http.Client. Defaults to a 30-second
	// timeout per request with TLS 1.3 minimum. Override before any
	// resource call to swap in a custom transport (e.g. proxy support).
	HTTPClient *http.Client

	// UserAgent is the value sent on every request. Defaults to
	// "go-terrapod/<version>". Override to differentiate downstream
	// tools (terraform-provider-terrapod, terrapod-migrate, etc.).
	UserAgent string

	// MaxRetries caps the number of retry attempts on 429 / 5xx /
	// transient transport errors. Defaults to 3.
	MaxRetries int
}

// Options configure a Client at construction time. All fields are
// optional except BaseURL and Token.
type Options struct {
	// BaseURL is the Terrapod base URL. Scheme is optional — defaults
	// to https when omitted. Trailing slash is tolerated and stripped.
	BaseURL string

	// Token is the Bearer token. Required.
	Token string

	// UserAgent overrides the default ("go-terrapod/<version>"). Set
	// this to identify the consuming tool — e.g. the migration tool
	// sets "terrapod-migrate/<version>" so server logs show which
	// caller drove a request.
	UserAgent string

	// SkipTLSVerify disables certificate verification. Only useful in
	// development against self-signed Terrapod deployments; never use
	// in production. Defaults to false.
	SkipTLSVerify bool

	// HTTPClient lets callers inject a custom http.Client (e.g.
	// behind a corporate proxy). When non-nil, SkipTLSVerify is
	// ignored — the caller's transport governs.
	HTTPClient *http.Client

	// MaxRetries overrides the default of 3.
	MaxRetries int

	// AllowInsecureTransport permits an http:// BaseURL. Without it, a
	// plaintext base URL is a construction error rather than a silent
	// downgrade (GHSA-5fh8-vj57-6gvh): every request the SDK makes carries
	// a long-lived platform bearer token, and over http:// it carries it in
	// the clear to anyone on the path. A scheme-less base still defaults to
	// https, so this only affects an operator who wrote http:// explicitly.
	//
	// Mirrors SkipTLSVerify: insecure transport has to be asked for.
	AllowInsecureTransport bool
}

// NewClient constructs a Client from Options. Returns an error if
// required fields are missing.
//
// The constructor does NOT contact the server — it's safe to build a
// Client and discard it. Use Client.VersionCheck if you want a
// fail-fast probe at startup.
func NewClient(opts Options) (*Client, error) {
	if opts.Token == "" {
		return nil, errors.New("terrapod: Token is required")
	}
	if opts.BaseURL == "" {
		return nil, errors.New("terrapod: BaseURL is required")
	}

	baseURL := normaliseBaseURL(opts.BaseURL)
	// The escape hatch has to be reachable without a code change. `Options` is
	// only settable by a Go caller, and NOTHING plumbs it: not the provider, not
	// terrapod-migrate, not terrapod-publish. Without this env var an operator
	// running an on-prem Terrapod behind a TLS-terminating load balancer at
	// http://terrapod.internal would have hit a hard failure on a PATCH release
	// with no way to opt back in.
	allowInsecure := opts.AllowInsecureTransport || os.Getenv("TERRAPOD_ALLOW_INSECURE_TRANSPORT") == "1"
	if strings.HasPrefix(baseURL, "http://") && !isLoopback(baseURL) && !allowInsecure {
		return nil, fmt.Errorf(
			"base URL %q uses http:// — the bearer token would cross the network in "+
				"the clear. Use https://, or set TERRAPOD_ALLOW_INSECURE_TRANSPORT=1 "+
				"to accept that risk deliberately",
			baseURL,
		)
	}
	hc := opts.HTTPClient
	if hc != nil && hc.CheckRedirect == nil {
		// An injected client got NO redirect protection, because CheckRedirect
		// was only set on the default one below. That is not a corner: the MCP
		// server supplies its own client (it needs a token-refreshing
		// transport), and so does the load-test harness — so the component that
		// runs unattended against a production instance was the one without the
		// fix. Copy rather than mutate: the caller owns their http.Client.
		clone := *hc
		clone.CheckRedirect = dropCredentialOnUnsafeRedirect
		hc = &clone
	}
	if hc == nil {
		transport := &http.Transport{
			TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS13}, //nolint:gosec
		}
		if opts.SkipTLSVerify {
			transport.TLSClientConfig.InsecureSkipVerify = true //nolint:gosec
		}
		hc = &http.Client{
			Transport:     transport,
			Timeout:       30 * time.Second,
			CheckRedirect: dropCredentialOnUnsafeRedirect,
		}
	}

	ua := opts.UserAgent
	if ua == "" {
		ua = "go-terrapod/" + SDKVersion
	}
	retries := opts.MaxRetries
	if retries <= 0 {
		retries = 3
	}

	return &Client{
		BaseURL:    baseURL,
		Token:      opts.Token,
		HTTPClient: hc,
		UserAgent:  ua,
		MaxRetries: retries,
	}, nil
}

// SDKVersion is the build-time-pinned SDK version. Used for the
// default User-Agent and as the "tool" argument to VersionCheck. The
// release pipeline overrides this via -ldflags="-X
// github.com/mattrobinsonsre/terrapod/go-terrapod.SDKVersion=v0.27.0";
// the default "dev" identifies development builds.
var SDKVersion = "dev"

// isLoopback reports whether the base URL addresses this machine.
//
// The risk http:// carries is the token crossing a network in the clear, and
// loopback is not a network — nothing between the two ends can observe it. So
// plaintext to 127.0.0.1 / ::1 / localhost needs no opt-in, which also keeps
// every httptest-backed caller working without weakening the gate.
//
// Deliberately NOT extended to private ranges: http:// to another pod or host
// on 10.0.0.0/8 does cross a network, and that is the case the gate is for.
func isLoopback(baseURL string) bool {
	u, err := url.Parse(baseURL)
	if err != nil {
		return false
	}
	host := u.Hostname()
	if strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

// dropCredentialOnUnsafeRedirect strips Authorization from a redirect that
// leaves the original host or drops out of TLS (GHSA-5fh8-vj57-6gvh).
//
// Go's stdlib already strips the header on a cross-DOMAIN redirect, which is
// why "302 to attacker.tld" is not the finding. But its rule is
// isDomainOrSubdomain on the hostname alone: it ignores scheme and port, so
// both of these carried the platform token —
//
//	https://terrapod.example.com -> http://terrapod.example.com       (cleartext)
//	https://terrapod.example.com -> https://evil.terrapod.example.com (subdomain)
//
// and this is concretely reachable: GetRunPlanJSON documents that the endpoint
// 302s to a presigned storage URL which the client follows, so a deployment
// whose object storage sits on a subdomain of the API host handed the token to
// the storage tier on every plan-JSON fetch.
//
// It STRIPS rather than refuses, deliberately. Refusing any host change — the
// reported suggestion — would break that documented redirect, because presigned
// storage genuinely lives on another host. The presigned URL carries its own
// signature in the query string and never needed our bearer, so dropping the
// header keeps the fetch working and takes the credential out of it.
func dropCredentialOnUnsafeRedirect(req *http.Request, via []*http.Request) error {
	if len(via) >= 5 {
		return fmt.Errorf("stopped after %d redirects", len(via))
	}
	origin := via[0].URL
	if !sameAuthority(req.URL, origin) || isSchemeDowngrade(origin, req.URL) {
		req.Header.Del("Authorization")
	}
	return nil
}

// sameAuthority compares host AND port. Comparing Hostname() alone ignored the
// port, so a redirect to another service on the same host — a registry, a
// preview app, a metrics UI — kept the platform token.
func sameAuthority(a, b *url.URL) bool {
	return strings.EqualFold(a.Hostname(), b.Hostname()) && portOf(a) == portOf(b)
}

func portOf(u *url.URL) string {
	if p := u.Port(); p != "" {
		return p
	}
	if strings.EqualFold(u.Scheme, "http") {
		return "80"
	}
	return "443"
}

// isSchemeDowngrade reports a move to weaker transport than the request started
// on. Testing `!= "https"` instead punished a deployment that is legitimately
// plaintext — the loopback carve-out permits http://127.0.0.1, and a bare
// trailing-slash 301 from that server back to itself would have had its
// credential stripped and 401'd, where the previous release worked.
func isSchemeDowngrade(from, to *url.URL) bool {
	return strings.EqualFold(from.Scheme, "https") && !strings.EqualFold(to.Scheme, "https")
}

// normaliseBaseURL prepends https:// when the scheme is missing, trims
// trailing slashes, and returns the result. Operator-friendly input
// like "terrapod.example.com" or "https://terrapod.example.com/" both
// produce "https://terrapod.example.com".
func normaliseBaseURL(raw string) string {
	url := strings.TrimSpace(raw)
	if !strings.HasPrefix(url, "http://") && !strings.HasPrefix(url, "https://") {
		url = "https://" + url
	}
	return strings.TrimRight(url, "/")
}

// Get performs a GET against path and returns the decoded response
// body, or a typed error. The path is appended to Client.BaseURL.
//
// Use the resource-specific methods (Client.GetWorkspace, etc.) where
// available; Get is the low-level fallback for endpoints the SDK
// hasn't grown a typed method for yet.
func (c *Client) Get(ctx context.Context, path string) ([]byte, error) {
	body, status, err := c.do(ctx, http.MethodGet, path, nil)
	if err != nil {
		return nil, err
	}
	if status < 200 || status >= 300 {
		return nil, classifyError(status, body)
	}
	return body, nil
}

// Post performs a POST.
func (c *Client) Post(ctx context.Context, path string, payload []byte) ([]byte, error) {
	body, status, err := c.do(ctx, http.MethodPost, path, payload)
	if err != nil {
		return nil, err
	}
	if status < 200 || status >= 300 {
		return nil, classifyError(status, body)
	}
	return body, nil
}

// Patch performs a PATCH.
func (c *Client) Patch(ctx context.Context, path string, payload []byte) ([]byte, error) {
	body, status, err := c.do(ctx, http.MethodPatch, path, payload)
	if err != nil {
		return nil, err
	}
	if status < 200 || status >= 300 {
		return nil, classifyError(status, body)
	}
	return body, nil
}

// Put performs a PUT with the standard JSON:API Content-Type.
func (c *Client) Put(ctx context.Context, path string, payload []byte) ([]byte, error) {
	body, status, err := c.do(ctx, http.MethodPut, path, payload)
	if err != nil {
		return nil, err
	}
	if status < 200 || status >= 300 {
		return nil, classifyError(status, body)
	}
	return body, nil
}

// PutRaw performs a PUT with a caller-supplied Content-Type, used for
// non-JSON:API uploads such as state version content. Error responses
// are still expected to be JSON:API envelopes.
func (c *Client) PutRaw(ctx context.Context, path, contentType string, payload []byte) ([]byte, error) {
	body, status, err := c.doWithContentType(ctx, http.MethodPut, path, payload, contentType)
	if err != nil {
		return nil, err
	}
	if status < 200 || status >= 300 {
		return nil, classifyError(status, body)
	}
	return body, nil
}

// Delete performs a DELETE with no body.
func (c *Client) Delete(ctx context.Context, path string) error {
	body, status, err := c.do(ctx, http.MethodDelete, path, nil)
	if err != nil {
		return err
	}
	if status < 200 || status >= 300 {
		return classifyError(status, body)
	}
	return nil
}

// DeleteWithBody performs a DELETE that carries a request body. Most
// REST APIs reject this; Terrapod's bulk-remove endpoints accept it.
func (c *Client) DeleteWithBody(ctx context.Context, path string, payload []byte) error {
	body, status, err := c.do(ctx, http.MethodDelete, path, payload)
	if err != nil {
		return err
	}
	if status < 200 || status >= 300 {
		return classifyError(status, body)
	}
	return nil
}

// isIdempotent reports whether an HTTP method is safe to retry. GET,
// HEAD, OPTIONS, PUT, and DELETE are idempotent per RFC 7231 — replaying
// them after a timeout or 5xx yields the same server state. POST and
// PATCH are NOT: a retried POST/PATCH that the server already processed
// (but whose response was lost to a timeout or surfaced as a 5xx after a
// partial write) would double-write. The comparison is case-insensitive.
func isIdempotent(method string) bool {
	switch strings.ToUpper(method) {
	case http.MethodGet, http.MethodHead, http.MethodOptions, http.MethodPut, http.MethodDelete:
		return true
	default:
		return false
	}
}

// do is the inner request-with-retry workhorse. Retries are
// method-aware: only idempotent methods (GET/HEAD/OPTIONS/PUT/DELETE)
// are retried, since replaying a non-idempotent POST/PATCH that the
// server already processed risks a double-write. For idempotent methods
// it retries on:
//   - HTTP 429 (rate-limited) — exponential backoff capped at 4s
//   - HTTP 5xx — same backoff
//   - Transient transport errors — net.Error.Timeout() returning true,
//     or a context.DeadlineExceeded wrap; never on a "connection
//     refused" or unresolved-host (those are permanent for the
//     duration of the operator's run)
//
// For a non-idempotent method, the first attempt's outcome is returned
// as-is: a 5xx body comes back with its status and a nil error (so the
// caller's classifyError still runs), and a transient net error comes
// back as nil, 0, err.
//
// Returns the response body bytes, the HTTP status code, and any error.
// Body is always populated when err is nil regardless of status, so
// callers can produce typed errors from non-2xx bodies.
func (c *Client) do(ctx context.Context, method, path string, body []byte) ([]byte, int, error) {
	return c.doWithContentType(ctx, method, path, body, "application/vnd.api+json")
}

// doWithContentType is the same as do but allows the caller to
// override the request Content-Type. Used by raw uploads (e.g. state
// version content) that send non-JSON:API payloads. The Accept header
// stays application/vnd.api+json because error responses are still
// JSON:API envelopes. Retries are method-aware — see do.
func (c *Client) doWithContentType(ctx context.Context, method, path string, body []byte, contentType string) ([]byte, int, error) {
	var lastErr error
	for attempt := 0; attempt <= c.MaxRetries; attempt++ {
		if attempt > 0 {
			// Exponential backoff: 1s, 2s, 4s; capped by ctx.Done().
			backoff := time.Duration(math.Pow(2, float64(attempt-1))) * time.Second
			select {
			case <-ctx.Done():
				return nil, 0, ctx.Err()
			case <-time.After(backoff):
			}
		}

		// G4 (GHSA-5fh8-vj57-6gvh): string concatenation let a `path` that does
		// not start with "/" reach the AUTHORITY, not just the path —
		// "http://terrapod.example.com" + "@evil:9/loot" parses with
		// Host=evil:9 and the real host demoted to userinfo, sending the bearer
		// token to the attacker. Get/Post/Put/Delete are exported and
		// documented as the low-level fallback for endpoints without a typed
		// method, with third-party automation named as a consumer, so `path` is
		// not always ours. The typed methods all hardcode a leading "/api/…".
		if !strings.HasPrefix(path, "/") {
			return nil, 0, fmt.Errorf("request path %q must start with \"/\"", path)
		}
		reqURL := c.BaseURL + path
		var bodyReader io.Reader
		if body != nil {
			bodyReader = bytes.NewReader(body)
		}
		req, err := http.NewRequestWithContext(ctx, method, reqURL, bodyReader)
		if err != nil {
			return nil, 0, fmt.Errorf("build request: %w", err)
		}
		req.Header.Set("Authorization", "Bearer "+c.Token)
		req.Header.Set("Content-Type", contentType)
		req.Header.Set("Accept", "application/vnd.api+json")
		req.Header.Set("User-Agent", c.UserAgent)

		resp, err := c.HTTPClient.Do(req)
		if err != nil {
			// Only retry transient net errors on idempotent methods —
			// a timed-out POST/PATCH may have already been applied
			// server-side, so replaying it would double-write.
			if isTransientNetError(err) && isIdempotent(method) {
				lastErr = err
				continue
			}
			return nil, 0, err
		}
		// G5: bounded. The control already existed in this module —
		// version.go reads its discovery error body through an io.LimitReader —
		// so this was an inconsistency rather than an oversight. A hostile
		// server (or a MITM on a plaintext connection) could otherwise stream
		// until the consuming tool died; measured at 512 MiB read straight into
		// memory. The cap is far above any real JSON:API document.
		respBody, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBytes+1))
		_ = resp.Body.Close()
		if err != nil {
			return nil, resp.StatusCode, fmt.Errorf("read response: %w", err)
		}
		// Fail loudly rather than hand back a truncated document. GetRunPlanJSON
		// returns these bytes verbatim as "the raw JSON", and a plan for a few
		// thousand resources can genuinely exceed the cap — silently short JSON
		// surfaces as an unexplained parse error blamed on the server, or worse
		// is accepted by a tolerant consumer. Reading one byte past the limit is
		// what lets us tell "exactly at the cap" from "over it".
		if int64(len(respBody)) > maxResponseBytes {
			return nil, resp.StatusCode, fmt.Errorf(
				"response exceeds the %d-byte client limit; refusing a truncated body",
				maxResponseBytes,
			)
		}
		if (resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500) && isIdempotent(method) {
			// Sanitised: this Body is formatted into the retry-exhaustion
			// error an operator sees, so it is the same display surface.
			lastErr = &APIError{StatusCode: resp.StatusCode, Body: sanitiseErrorBody(respBody)}
			continue
		}
		return respBody, resp.StatusCode, nil
	}
	return nil, 0, fmt.Errorf("request failed after %d retries: %w", c.MaxRetries, lastErr)
}

// classifyError converts an HTTP error response to the most specific
// typed error available. Decodes the JSON:API error body when present
// so the operator-facing message is the one Terrapod intended.
func classifyError(statusCode int, body []byte) error {
	detail := extractErrorDetail(body)
	switch statusCode {
	case http.StatusUnauthorized:
		return &AuthenticationError{Detail: detail}
	case http.StatusForbidden:
		return &AuthorizationError{Detail: detail}
	case http.StatusNotFound:
		return &NotFoundError{}
	case http.StatusConflict:
		return &ConflictError{Detail: detail}
	case http.StatusUnprocessableEntity:
		return &ValidationError{Detail: detail}
	default:
		return &APIError{StatusCode: statusCode, Body: detail}
	}
}

// extractErrorDetail decodes Terrapod's JSON:API error body and
// concatenates per-error Detail strings (falling back to Title when
// Detail is empty). Returns the raw body verbatim if it isn't a
// JSON:API error envelope — callers see whatever the server actually
// said.
func extractErrorDetail(body []byte) string {
	var errResp ErrorResponse
	if err := json.Unmarshal(body, &errResp); err == nil && len(errResp.Errors) > 0 {
		parts := make([]string, 0, len(errResp.Errors))
		for _, e := range errResp.Errors {
			switch {
			case e.Detail != "":
				parts = append(parts, e.Detail)
			case e.Title != "":
				parts = append(parts, e.Title)
			}
		}
		if len(parts) > 0 {
			// Sanitise here too. Only the non-JSON:API fallback below was
			// cleaned, which is the UNCOMMON path — every request sends
			// `Accept: application/vnd.api+json`, so a real or MITM'd Terrapod
			// returns an envelope and its `detail` reached the operator's
			// terminal verbatim. The threat this control exists for is a
			// hostile server forging a confirmation prompt inside a
			// `terraform apply` transcript, and that is exactly the path it
			// was skipping.
			return sanitiseErrorBody([]byte(strings.Join(parts, "; ")))
		}
	}
	return sanitiseErrorBody(body)
}

// maxResponseBytes caps any single response the SDK will hold in memory.
const maxResponseBytes = 32 << 20 // 32 MiB

// maxErrorBodyBytes caps how much of a non-JSON:API body reaches an error
// string. errors.go documents these as intended for operator display.
const maxErrorBodyBytes = 512

// sanitiseErrorBody makes a non-JSON:API response body safe to print (G7).
//
// It was returned verbatim and uncapped. These strings are shown to operators —
// in a `terraform apply` transcript, say — so a hostile server could inject
// terminal escapes: clear the screen, set the window title, and forge a
// confirmation prompt. Control characters are replaced and the result is
// truncated.
func sanitiseErrorBody(body []byte) string {
	if len(body) > maxErrorBodyBytes {
		body = body[:maxErrorBodyBytes]
	}
	var b strings.Builder
	for _, r := range string(body) {
		switch {
		case r == '\n' || r == '\t':
			b.WriteRune(' ')
		case unicode.IsControl(r), unicode.Is(unicode.Cf, r), unicode.Is(unicode.Zl, r),
			unicode.Is(unicode.Zp, r):
			// Cf covers the bidirectional overrides (U+202E and the U+2066-2069
			// isolates) that drive Trojan-source text spoofing, and U+200B.
			// `unicode.IsControl` is category Cc only, so those passed through.
			b.WriteRune('?')
		default:
			b.WriteRune(r)
		}
	}
	return strings.TrimSpace(b.String())
}

// isTransientNetError categorises net errors into retryable / not.
// Timeouts are retryable; refused / unresolved aren't (within a single
// operator session). Reading ctx.DeadlineExceeded as transient lets a
// request-scoped timeout retry through the inner backoff before the
// outer ctx fires.
func isTransientNetError(err error) bool {
	if errors.Is(err, context.DeadlineExceeded) {
		return true
	}
	var netErr net.Error
	if errors.As(err, &netErr) {
		return netErr.Timeout()
	}
	return false
}
