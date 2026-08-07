package main

import (
	"testing"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// TestSDKVersionIsWired pins the one line that makes the version gate real.
//
// VersionCheck returns nil immediately when SDKVersion is "dev", SDKVersion
// defaults to "dev", and GoReleaser stamps main.Version — not the SDK's
// variable. So without the assignment every released binary skipped the check
// entirely (#1286).
//
// version_gate_test.go cannot catch that: its withSDKVersion helper overwrites
// the very variable the fix assigns, before each case. Delete the wiring and
// that table still goes green (#1297).
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
				"version gate is inert in every released binary",
			terrapod.SDKVersion,
		)
	}
}

// TestInitRanTheWiring — wireSDKVersion existing is no use if init() stops
// calling it. Package init has already run by the time a test executes, so
// this is the only window in which that is observable, and it must run before
// anything else reassigns the variable.
func TestInitRanTheWiring(t *testing.T) {
	if terrapod.SDKVersion != Version {
		t.Fatalf("SDKVersion = %q after package init, want %q", terrapod.SDKVersion, Version)
	}
}
