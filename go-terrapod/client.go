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
	"strings"
	"time"
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
	hc := opts.HTTPClient
	if hc == nil {
		transport := &http.Transport{
			TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS13}, //nolint:gosec
		}
		if opts.SkipTLSVerify {
			transport.TLSClientConfig.InsecureSkipVerify = true //nolint:gosec
		}
		hc = &http.Client{
			Transport: transport,
			Timeout:   30 * time.Second,
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

		url := c.BaseURL + path
		var bodyReader io.Reader
		if body != nil {
			bodyReader = bytes.NewReader(body)
		}
		req, err := http.NewRequestWithContext(ctx, method, url, bodyReader)
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
		respBody, err := io.ReadAll(resp.Body)
		_ = resp.Body.Close()
		if err != nil {
			return nil, resp.StatusCode, fmt.Errorf("read response: %w", err)
		}
		if (resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500) && isIdempotent(method) {
			lastErr = &APIError{StatusCode: resp.StatusCode, Body: string(respBody)}
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
			return strings.Join(parts, "; ")
		}
	}
	return string(body)
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
