// Package tfprovider is the publicly importable handle on the Terraform
// provider, and it exists for exactly one reason: Go's `internal` rule.
//
// `provider/internal/provider` is importable only by code rooted at
// `github.com/mattrobinsonsre/terrapod/provider`, so the Pulumi provider
// (#1987) -- a sibling module, because the bridge's dependency tree has no
// business landing on this thin wrapper -- cannot reach `New` directly. This
// package re-exports it and nothing else.
//
// **It is deliberately not a second surface.** It adds no types, no options and
// no behaviour; it forwards one constructor. The moment anything else is added
// here it becomes a thing to keep in step with the provider, which is the drift
// the API-consumer contract exists to prevent -- and the Pulumi provider is
// bridged from this same Terraform schema precisely so that no such second
// surface exists.
package tfprovider

import (
	"github.com/hashicorp/terraform-plugin-framework/provider"

	internalprovider "github.com/mattrobinsonsre/terrapod/provider/internal/provider"
)

// New returns the Terraform provider constructor, as `main.go` consumes it.
//
// `version` is threaded through to `terrapod.SDKVersion`, which go-terrapod
// sends as the tool argument to its API version check -- so a bridged caller
// passing its own version makes the Pulumi provider identifiable to the server
// rather than impersonating the Terraform one.
func New(version string) func() provider.Provider {
	return internalprovider.New(version)
}
