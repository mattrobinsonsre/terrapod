package provider

import (
	"testing"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// TestSDKVersionIsWired pins the one line that makes the version gate real.
//
// VersionCheck returns nil immediately when SDKVersion is "dev", SDKVersion
// defaults to "dev", and GoReleaser stamps main.version — not the SDK's
// variable. So without the assignment in New() the gate is inert in every
// released provider while every other test still passes, which is exactly the
// bug this file exists to stop recurring (#1287, #1297).
//
// The schema-contract test already calls New("test"); it asserts nothing about
// this, so deleting the assignment left the whole suite green.
func TestSDKVersionIsWired(t *testing.T) {
	prev := terrapod.SDKVersion
	t.Cleanup(func() { terrapod.SDKVersion = prev })

	terrapod.SDKVersion = "not-set-by-new"
	New("v9.9.9")

	if terrapod.SDKVersion != "v9.9.9" {
		t.Fatalf(
			"SDKVersion = %q after New(\"v9.9.9\") — the assignment in New() is missing, "+
				"so Configure's compatibility check can never fire in a released provider",
			terrapod.SDKVersion,
		)
	}
}

// TestSourceBuildStaysUncompared — "dev" is how the SDK is told it cannot
// compare. Local provider development must keep working, so the wiring must
// pass the value through rather than substituting something comparable.
func TestSourceBuildStaysUncompared(t *testing.T) {
	prev := terrapod.SDKVersion
	t.Cleanup(func() { terrapod.SDKVersion = prev })

	New("dev")
	if terrapod.SDKVersion != "dev" {
		t.Fatalf("SDKVersion = %q, want \"dev\" passed straight through", terrapod.SDKVersion)
	}
}
