package terrapod

import (
	"testing"

	terrapodsdk "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// TestASourceBuildTELLS_THE_SDK_DEV guards a defect this very file introduced.
//
// Two consumers want different things from one version string, and they conflict
// for a source build:
//
//   - the BRIDGE validates `ProviderInfo.Version` as semver and refuses to serve
//     or generate anything it cannot parse, so a source build cannot be the
//     Terraform provider's bare `dev`;
//   - `go-terrapod`'s `VersionCheck` skips the SDK-vs-API compatibility probe
//     ONLY for the literal `dev` or an empty string.
//
// So handing the SDK `0.0.0-dev` makes it perform a REAL comparison against the
// API's version and report a mismatch — a locally built plugin failing with a
// version error that has nothing to do with Pulumi. The Terraform provider's own
// `New` docstring protects exactly this case by passing `dev`; the Pulumi side
// has to translate instead, because it cannot pass `dev` upward.
func TestASourceBuildTellsTheSDKDev(t *testing.T) {
	if got := sdkVersion(SourceBuildVersion); got != "dev" {
		t.Errorf("sdkVersion(%q) = %q, want %q — VersionCheck skips only the literal "+
			"\"dev\", so anything else makes a source build probe the API and report a "+
			"mismatch", SourceBuildVersion, got, "dev")
	}
	if got := sdkVersion(""); got != "dev" {
		t.Errorf("sdkVersion(\"\") = %q, want \"dev\"", got)
	}
}

// TestAReleaseBuildTellsTheSDKTheRealVersion is the other half, and without it
// the fix above could be "always return dev" — which would silently disable the
// compatibility check for every operator, the opposite failure and a worse one.
func TestAReleaseBuildTellsTheSDKTheRealVersion(t *testing.T) {
	for _, v := range []string{"1.11.1", "2.0.0", "1.0.0-rc.1"} {
		if got := sdkVersion(v); got != v {
			t.Errorf("sdkVersion(%q) = %q, want it unchanged — an operator's build must "+
				"perform the SDK/API compatibility check", v, got)
		}
	}
}

// TestTheSentinelIsSEMVER, because the bridge rejects anything else and the
// failure would be at the first invocation of either command rather than at
// release time.
func TestTheSentinelIsSemver(t *testing.T) {
	// Minimal check rather than a semver library: the bridge wants MAJOR.MINOR.PATCH
	// with an optional prerelease, and what matters here is that the sentinel has
	// not been "simplified" back to a bare word.
	if SourceBuildVersion == "dev" || SourceBuildVersion == "" {
		t.Fatalf("SourceBuildVersion = %q, which the bridge cannot parse; it validates "+
			"this and refuses to generate or serve", SourceBuildVersion)
	}
	for _, c := range []byte{'.', '0'} {
		found := false
		for i := 0; i < len(SourceBuildVersion); i++ {
			if SourceBuildVersion[i] == c {
				found = true
				break
			}
		}
		if !found {
			t.Errorf("SourceBuildVersion = %q does not look like semver", SourceBuildVersion)
		}
	}
}

// TestProviderThreadsTheTranslatedVersionIntoTheSDK is the call-site half: the
// translation is only worth anything if `Provider` actually uses it. A presence
// check on the helper would pass with the call site handing over `version`.
func TestProviderThreadsTheTranslatedVersionIntoTheSDK(t *testing.T) {
	before := terrapodsdk.SDKVersion
	t.Cleanup(func() { terrapodsdk.SDKVersion = before })

	terrapodsdk.SDKVersion = "poisoned"
	_ = Provider(SourceBuildVersion)
	if terrapodsdk.SDKVersion != "dev" {
		t.Errorf("after Provider(%q) the SDK version is %q, want \"dev\" — the call site "+
			"is not passing the translated value", SourceBuildVersion, terrapodsdk.SDKVersion)
	}

	terrapodsdk.SDKVersion = "poisoned"
	_ = Provider("1.11.1")
	if terrapodsdk.SDKVersion != "1.11.1" {
		t.Errorf("after Provider(\"1.11.1\") the SDK version is %q, want \"1.11.1\"",
			terrapodsdk.SDKVersion)
	}
}
