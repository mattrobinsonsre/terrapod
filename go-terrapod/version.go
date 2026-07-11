package terrapod

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
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
var ErrVersionUnreported = errors.New("version not reported by Terrapod in .well-known/terraform.json")

// versionProbeTimeout caps the discovery GET: generous enough for a
// healthy API, short enough that a misconfigured target fails fast.
const versionProbeTimeout = 10 * time.Second

// VersionCheck probes the target Terrapod deployment's reported
// version and compares it against the SDK's build-time-pinned
// SDKVersion using the support policy: compatible when they share the
// same MAJOR and the API is at least as new as the SDK (api >= sdk).
// Returns nil when compatible (or when either side is a dev build),
// wrapped ErrVersionMismatch on an incompatible major / too-old API,
// ErrVersionUnreported when the deployment doesn't report a version,
// or a network/HTTP error.
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
		// Dev builds have no pinned version to compare against — the
		// compatibility check can't be performed, so skip it silently
		// rather than fail. A release build always has a real SDKVersion.
		return nil
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
	defer func() { _ = resp.Body.Close() }()
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
	if api == "dev" {
		// The target is a dev build of Terrapod — no meaningful version to
		// compare against; skip rather than false-alarm.
		return nil
	}
	if compatible, reason := versionsCompatible(SDKVersion, api); !compatible {
		return fmt.Errorf("%w: SDK=%s API=%s (%s) — install a matching SDK/provider release: https://github.com/mattrobinsonsre/terrapod/releases",
			ErrVersionMismatch, SDKVersion, api, reason)
	}
	return nil
}

// parseSemver parses a "MAJOR.MINOR.PATCH" string (ignoring any leading "v" and
// any "-prerelease"/"+build" suffix) into its numeric parts. ok is false if the
// string isn't three dot-separated integers.
func parseSemver(v string) (major, minor, patch int, ok bool) {
	v = strings.TrimPrefix(strings.TrimSpace(v), "v")
	if i := strings.IndexAny(v, "-+"); i >= 0 {
		v = v[:i]
	}
	parts := strings.SplitN(v, ".", 3)
	if len(parts) != 3 {
		return 0, 0, 0, false
	}
	var err error
	if major, err = strconv.Atoi(parts[0]); err != nil {
		return 0, 0, 0, false
	}
	if minor, err = strconv.Atoi(parts[1]); err != nil {
		return 0, 0, 0, false
	}
	if patch, err = strconv.Atoi(parts[2]); err != nil {
		return 0, 0, 0, false
	}
	return major, minor, patch, true
}

// versionsCompatible implements the go-terrapod support policy: an SDK built at
// version `sdk` works against an API at version `api` when they share the same
// MAJOR and the API is at least as new as the SDK (`api >= sdk`). A newer API is
// forward-compatible within the major; an older API may be missing endpoints the
// SDK expects, and a different major is a hard break. If either version is
// unparseable, treat as compatible (fail-open) so a non-semver tag never blocks.
func versionsCompatible(sdk, api string) (ok bool, reason string) {
	sMaj, sMin, sPat, sOK := parseSemver(sdk)
	aMaj, aMin, aPat, aOK := parseSemver(api)
	if !sOK || !aOK {
		return true, ""
	}
	if aMaj != sMaj {
		return false, fmt.Sprintf("incompatible major version — SDK is v%d.x, API is v%d.x", sMaj, aMaj)
	}
	sTuple := [3]int{sMaj, sMin, sPat}
	aTuple := [3]int{aMaj, aMin, aPat}
	for i := range 3 {
		if aTuple[i] != sTuple[i] {
			if aTuple[i] < sTuple[i] {
				return false, "API is older than the SDK was built against; upgrade the Terrapod deployment or use an older SDK/provider"
			}
			return true, "" // API newer within the same major — forward-compatible
		}
	}
	return true, "" // exact match
}
