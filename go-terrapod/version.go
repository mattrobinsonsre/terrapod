package terrapod

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// DiscoveryPath is the well-known service-discovery endpoint Terrapod
// serves at every deployment. Both terraform/tofu and tfci speak to
// it, so SDK callers see consistent behaviour with the rest of the
// Terrapod tooling.
const DiscoveryPath = "/.well-known/terraform.json"

// VersionField is the JSON key in the discovery doc carrying the
// running Terrapod API version (e.g. "0.27.0"). Terrapod started
// emitting it in v0.24.
const VersionField = "terrapod-version"

// ErrVersionMismatch is returned by VersionCheck when the SDK's
// build-time-pinned version doesn't match the API's reported version.
// The wrapped error message names both versions and a link to the
// matching SDK release so operators have an actionable next step.
var ErrVersionMismatch = errors.New("SDK version does not match Terrapod API version")

// ErrVersionUnreported is returned when the discovery doc was
// reachable but didn't carry the version field — typically a Terrapod
// deployment older than v0.24, or a malformed deployment. Surfaced
// separately so the operator action ("upgrade the target Terrapod")
// is distinct from a version-mismatch.
var ErrVersionUnreported = errors.New("Terrapod did not report a version in .well-known/terraform.json")

// versionProbeTimeout caps the discovery GET: generous enough for a
// healthy API, short enough that a misconfigured target fails fast.
const versionProbeTimeout = 10 * time.Second

// VersionCheck probes the target Terrapod deployment's reported
// version and compares it against the SDK's build-time-pinned
// SDKVersion. Returns nil on exact match, wrapped ErrVersionMismatch
// or ErrVersionUnreported otherwise, or a network/HTTP error.
//
// Most callers should invoke this once at startup (after constructing
// a Client) as a fail-fast probe before any further work. Tools that
// need to override the version pin (development builds, hot-patches
// within a patch series) should match on errors.Is(err, ErrVersionMismatch)
// and prompt the operator before continuing.
//
//	client, err := terrapod.NewClient(terrapod.Options{
//	    BaseURL: "https://terrapod.example.com", Token: tok,
//	})
//	if err != nil { ... }
//	if err := client.VersionCheck(ctx); err != nil {
//	    if errors.Is(err, terrapod.ErrVersionMismatch) {
//	        // print operator-facing warning, prompt, or override flag
//	    }
//	    return err
//	}
//
// Tests substitute the HTTP client by passing a custom HTTPClient in
// terrapod.Options when constructing the Client.
func (c *Client) VersionCheck(ctx context.Context) error {
	if SDKVersion == "" || SDKVersion == "dev" {
		// Dev builds have no meaningful version to compare against.
		// Surface as Mismatch (not Unreported) — the operator action
		// is "use a tagged SDK build, or pass an override flag in
		// the consuming tool".
		return fmt.Errorf("%w: SDK version is %q (likely a development build); use a tagged SDK build or pass --allow-api-version-mismatch in the consuming tool",
			ErrVersionMismatch, SDKVersion)
	}

	url := c.BaseURL + DiscoveryPath
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return fmt.Errorf("build discovery request: %w", err)
	}
	// Wrap the Client's HTTPClient with a short per-request timeout so
	// the probe fails fast even when the caller's HTTPClient has no
	// timeout configured. Callers can override by constructing the
	// Client with a custom HTTPClient.
	httpClient := c.HTTPClient
	if httpClient == nil {
		httpClient = &http.Client{Timeout: versionProbeTimeout}
	} else if httpClient.Timeout == 0 || httpClient.Timeout > versionProbeTimeout {
		clone := *httpClient
		clone.Timeout = versionProbeTimeout
		httpClient = &clone
	}
	resp, err := httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("fetch %s: %w", url, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("discovery: %s returned %d: %s",
			url, resp.StatusCode, strings.TrimSpace(string(body)))
	}
	var doc map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&doc); err != nil {
		return fmt.Errorf("decode discovery doc: %w", err)
	}
	rawAPI, ok := doc[VersionField]
	if !ok {
		return ErrVersionUnreported
	}
	api, ok := rawAPI.(string)
	if !ok || api == "" {
		return ErrVersionUnreported
	}
	if api != SDKVersion {
		return fmt.Errorf("%w: SDK=%s API=%s — install the matching release: https://github.com/mattrobinsonsre/terrapod/releases/tag/v%s",
			ErrVersionMismatch, SDKVersion, api, api)
	}
	return nil
}

