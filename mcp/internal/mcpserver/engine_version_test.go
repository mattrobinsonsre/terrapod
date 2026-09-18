package mcpserver

import (
	"strings"
	"testing"
)

// The workspace tools take the engine version under either name (#1559):
// `engine_version` is canonical, `terraform_version` is the name the terraform
// CLI tooling uses and the API accepts indefinitely.
func TestEngineVersionIn(t *testing.T) {
	cases := []struct {
		name        string
		engine      string
		terraform   string
		wantVersion string
		wantErr     bool
	}{
		{
			name:        "canonical only",
			engine:      "1.12",
			wantVersion: "1.12",
		},
		{
			name:        "original name only still works",
			terraform:   "1.11",
			wantVersion: "1.11",
		},
		{
			name:        "both, agreeing — redundant, not a conflict",
			engine:      "1.12",
			terraform:   "1.12",
			wantVersion: "1.12",
		},
		{
			// Only one key reaches the server, so a disagreeing pair would be
			// resolved silently and the agent would never learn which won.
			name:      "both, disagreeing is refused rather than resolved",
			engine:    "1.12",
			terraform: "1.11",
			wantErr:   true,
		},
		{
			name: "neither leaves the server default alone",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, errMsg := engineVersionIn(tc.engine, tc.terraform)
			if (errMsg != "") != tc.wantErr {
				t.Fatalf("errMsg = %q, wantErr %t", errMsg, tc.wantErr)
			}
			if tc.wantErr {
				// The message has to name both fields, or the agent cannot tell
				// which of its inputs to change.
				for _, want := range []string{"engine_version", "terraform_version"} {
					if !strings.Contains(errMsg, want) {
						t.Errorf("error does not mention %s: %s", want, errMsg)
					}
				}
				return
			}
			if got != tc.wantVersion {
				t.Errorf("version = %q, want %q", got, tc.wantVersion)
			}
		})
	}
}
