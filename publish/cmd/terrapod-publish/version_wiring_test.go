package main

import (
	"testing"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// TestSDKVersionIsWired pins the one line that makes the version gate real.
//
// VersionCheck returns nil immediately when SDKVersion is "dev", SDKVersion
// defaults to "dev", and GoReleaser stamps main.Version — not the SDK's
// variable. So without the assignment the gate is inert in every released
// binary while every other test stays green, which is the bug this file exists
// to stop recurring (#1287, #1297).
//
// It drives wireSDKVersion() from a sentinel rather than reading the variable
// after init(): in a source build both values are "dev", so a plain equality
// check would hold whether or not the assignment exists.
func TestSDKVersionIsWired(t *testing.T) {
	prev := terrapod.SDKVersion
	t.Cleanup(func() { terrapod.SDKVersion = prev })
	origVersion := Version
	t.Cleanup(func() { Version = origVersion })

	terrapod.SDKVersion = "not-wired"
	Version = "v9.9.9"
	wireSDKVersion()

	if terrapod.SDKVersion != "v9.9.9" {
		t.Fatalf(
			"SDKVersion = %q after wireSDKVersion() — the assignment is missing, so the "+
				"compatibility warning can never fire in a released build",
			terrapod.SDKVersion,
		)
	}
}

// TestInitRanTheWiring — wireSDKVersion existing is no use if init() stops
// calling it. Package init has already run by the time a test executes, so
// this is the only window in which that is observable.
func TestInitRanTheWiring(t *testing.T) {
	if terrapod.SDKVersion != Version {
		t.Fatalf("SDKVersion = %q after package init, want %q", terrapod.SDKVersion, Version)
	}
}
