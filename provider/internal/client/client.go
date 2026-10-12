// Package client carries the provider's ProviderData type: a tiny
// shim around the constructor parameters (BaseURL, Token,
// SkipTLSVerify, DefaultWorkspace). All actual HTTP + JSON:API work lives in
// github.com/mattrobinsonsre/terrapod/go-terrapod — each resource and
// data source builds a *terrapod.Client from this shim in its
// Configure callback. (#347)
//
// We keep the shim instead of passing a *terrapod.Client directly so
// resources can keep deferring SDK construction to per-call sites,
// where they can wire in resource-specific retry policy / user-agent
// strings if needed. Replacing this with a plain struct also avoids
// the framework holding a long-lived *http.Client across all
// resources, which keeps test isolation simple.
package client

import "strings"

// Client is the bare configuration passed from provider.Configure to
// each resource/data source as ProviderData. It carries no HTTP
// machinery of its own.
type Client struct {
	BaseURL       string
	Token         string
	SkipTLSVerify bool

	// DefaultWorkspace is the workspace a resource uses when its own
	// `workspace_id` is not configured — empty when there is none.
	//
	// It lives here rather than in a wider ProviderData type because this
	// struct IS the provider configuration every resource already receives,
	// so adding a field changes none of the Configure callbacks and nothing
	// in the public SDK. Inside a run the runner exports TERRAPOD_WORKSPACE
	// (runner/phases/provider_credentials.py) from the run's own workspace,
	// which is what lets an operator write `provider "terrapod" {}` with
	// nothing in it and declare inventory without threading an id through
	// (#2036).
	DefaultWorkspace string
}

// NewClient builds the shim from raw provider configuration. The
// hostname is normalised the same way go-terrapod does (default
// https://, trim trailing slash) so resources building SDK clients
// from a Client get a stable BaseURL.
func NewClient(hostname, token string, skipTLSVerify bool, defaultWorkspace string) *Client {
	baseURL := hostname
	if !strings.HasPrefix(baseURL, "http://") && !strings.HasPrefix(baseURL, "https://") {
		baseURL = "https://" + baseURL
	}
	baseURL = strings.TrimSuffix(baseURL, "/")

	return &Client{
		BaseURL:          baseURL,
		Token:            token,
		SkipTLSVerify:    skipTLSVerify,
		DefaultWorkspace: defaultWorkspace,
	}
}
