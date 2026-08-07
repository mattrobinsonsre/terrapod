package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// discoveryServer serves the well-known doc the version probe reads.
// apiVersion == "" omits the field, which is how a Terrapod older than
// v0.24 looks on the wire.
func discoveryServer(t *testing.T, apiVersion string) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != terrapod.DiscoveryPath {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		if apiVersion == "" {
			_, _ = w.Write([]byte(`{"state.v2":"/api/v2/"}`))
			return
		}
		_, _ = w.Write([]byte(`{"state.v2":"/api/v2/","` + terrapod.VersionField + `":"` + apiVersion + `"}`))
	}))
	t.Cleanup(srv.Close)
	return srv
}

func clientFor(t *testing.T, baseURL string) *terrapod.Client {
	t.Helper()
	c, err := terrapod.NewClient(terrapod.Options{BaseURL: baseURL, Token: "t"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	return c
}

// withSDKVersion pins the SDK's build-time version for one test. The gate
// is a no-op on a dev build, so a test that forgot this would pass no
// matter what checkAPIVersion did — which is exactly how the real gate sat
// inert through every release (#1286).
func withSDKVersion(t *testing.T, v string) {
	t.Helper()
	prev := terrapod.SDKVersion
	terrapod.SDKVersion = v
	t.Cleanup(func() { terrapod.SDKVersion = prev })
}

func TestCheckAPIVersion(t *testing.T) {
	for _, tc := range []struct {
		name       string
		sdkVersion string
		apiVersion string
		allow      bool
		wantRefuse bool
	}{
		// The happy path, and the reason a source build still runs.
		{"compatible api runs", "v1.3.0", "v1.3.0", false, false},
		{"newer api is compatible", "v1.3.0", "v1.4.0", false, false},
		{"dev build skips the gate", "dev", "v1.3.0", false, false},

		// A real mismatch is refused by default and allowed by the flag.
		{"older api refused", "v1.3.0", "v1.2.0", false, true},
		{"older api allowed by flag", "v1.3.0", "v1.2.0", true, false},
		{"major mismatch refused", "v1.3.0", "v2.0.0", false, true},
		{"major mismatch allowed by flag", "v1.3.0", "v2.0.0", true, false},

		// A target too old to report its version can't be verified, so it
		// gets the same treatment rather than a silent pass.
		{"unreported version refused", "v1.3.0", "", false, true},
		{"unreported version allowed by flag", "v1.3.0", "", true, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			withSDKVersion(t, tc.sdkVersion)
			srv := discoveryServer(t, tc.apiVersion)
			err := checkAPIVersion(clientFor(t, srv.URL), tc.allow)

			if tc.wantRefuse && err == nil {
				t.Fatal("expected the run to be refused, got nil")
			}
			if !tc.wantRefuse && err != nil {
				t.Fatalf("expected the run to proceed, got: %v", err)
			}
			// A refusal nobody can act on is a support ticket.
			if err != nil && !strings.Contains(err.Error(), "--allow-api-version-mismatch") {
				t.Errorf("refusal must name the override flag, got: %v", err)
			}
		})
	}
}

// A probe that cannot reach the API must never block a migration — that
// was the whole rationale for the old warn-only behaviour, and it is the
// one case worth keeping.
func TestCheckAPIVersion_ProbeFailureNeverBlocks(t *testing.T) {
	withSDKVersion(t, "v1.3.0")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	t.Cleanup(srv.Close)

	if err := checkAPIVersion(clientFor(t, srv.URL), false); err != nil {
		t.Fatalf("an unreachable/500 probe must not refuse the run, got: %v", err)
	}
}
