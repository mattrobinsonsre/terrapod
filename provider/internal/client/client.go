// Package client carries the provider's ProviderData type: a tiny
// shim around the constructor parameters (BaseURL, Token,
// SkipTLSVerify). All actual HTTP + JSON:API work lives in
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
}

// NewClient builds the shim from raw provider configuration. The
// hostname is normalised the same way go-terrapod does (default
// https://, trim trailing slash) so resources building SDK clients
// from a Client get a stable BaseURL.
func NewClient(hostname, token string, skipTLSVerify bool) *Client {
	baseURL := hostname
	if !strings.HasPrefix(baseURL, "http://") && !strings.HasPrefix(baseURL, "https://") {
		baseURL = "https://" + baseURL
	}
	baseURL = strings.TrimSuffix(baseURL, "/")

	return &Client{
		BaseURL:       baseURL,
		Token:         token,
		SkipTLSVerify: skipTLSVerify,
	}
}
