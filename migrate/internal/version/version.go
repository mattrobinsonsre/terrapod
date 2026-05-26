// Package version implements the strict tool↔API version match gate
// described in issue #347.
//
// The migration tool writes a LOT of state (workspaces, variables, runs,
// state files, registry tarballs). The API's request/response shapes,
// validation rules, and default values drift between Terrapod releases.
// Running a tool built against Terrapod vN against an API serving vN+1
// risks subtle, hard-to-rollback corruption — a renamed JSON:API
// attribute that the tool sets to the old name silently lands a NULL,
// a stricter validator on the API side rejects half a migration midway,
// etc. The mitigation is to refuse to run by default when the tool's
// build-time version does not exactly match the API's reported version.
//
// The check is done once at startup against /.well-known/terraform.json,
// which Terrapod already serves (since v0.24 it reports the running API
// version under the `terrapod-version` key). On mismatch, the tool exits
// with a loud message naming the matching release tag and a link to the
// GitHub Release page. Operators with a deliberate reason to bypass
// (e.g. running a dev build, hot-patching a v0.27.x→v0.27.y) can pass
// --allow-api-version-mismatch.
package version

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
// serves. Both terraform/tofu and tfci already speak to it, so reading it
// from a migration tool is consistent with the wider Terrapod surface.
const DiscoveryPath = "/.well-known/terraform.json"

// VersionField is the JSON key in the discovery doc that carries the
// running Terrapod API's semver (e.g. "0.26.0"). Terrapod started
// emitting it in v0.24.
const VersionField = "terrapod-version"

// ErrMismatch is returned by Check when the tool's pinned version does
// not exactly match the API's reported version. Wrapping with %w lets the
// caller decide whether to honour --allow-api-version-mismatch.
var ErrMismatch = errors.New("tool version does not match API version")

// ErrUnreported is returned when the discovery doc was reachable but did
// not carry the version field — typically a Terrapod older than v0.24
// or a malformed deployment. Treated separately from ErrMismatch because
// the resolution (upgrade the target Terrapod) is different.
var ErrUnreported = errors.New("target Terrapod did not report a version in .well-known/terraform.json")

// httpClient is package-level so tests can swap it for a fake. The 10s
// timeout is generous for a single GET against a healthy API and short
// enough that a misconfigured --target host fails fast.
var httpClient = &http.Client{Timeout: 10 * time.Second}

// Check fetches the target's /.well-known/terraform.json and compares
// the reported `terrapod-version` against tool. Returns:
//
//   - nil on exact match.
//   - ErrMismatch (wrapped, formatted) when the values differ. The wrapped
//     error embeds the values so the operator-facing message names both.
//   - ErrUnreported when the field is missing or empty.
//   - A network/HTTP error otherwise, returned verbatim.
//
// targetBase is the Terrapod base URL without the path (e.g.
// "https://terrapod.example.com"); the function appends DiscoveryPath.
func Check(ctx context.Context, targetBase, tool string) error {
	if tool == "" {
		// No build-time pin available (running a `go run` from a checkout).
		// Refuse to bypass silently — the caller must pass
		// --allow-api-version-mismatch deliberately.
		return fmt.Errorf("%w: tool version is unset (likely a dev build); pass --allow-api-version-mismatch to override", ErrMismatch)
	}

	url := strings.TrimRight(targetBase, "/") + DiscoveryPath
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return fmt.Errorf("build discovery request: %w", err)
	}
	resp, err := httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("fetch %s: %w", url, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("discovery: %s returned %d: %s", url, resp.StatusCode, strings.TrimSpace(string(body)))
	}
	var doc map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&doc); err != nil {
		return fmt.Errorf("decode discovery doc: %w", err)
	}
	rawAPI, ok := doc[VersionField]
	if !ok {
		return ErrUnreported
	}
	api, ok := rawAPI.(string)
	if !ok || api == "" {
		return ErrUnreported
	}
	if api != tool {
		return fmt.Errorf("%w: tool=%s api=%s — install the matching release: https://github.com/mattrobinsonsre/terrapod/releases/tag/v%s",
			ErrMismatch, tool, api, api)
	}
	return nil
}
