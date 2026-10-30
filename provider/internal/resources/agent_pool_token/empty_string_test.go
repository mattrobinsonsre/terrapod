package agent_pool_token

import (
	"testing"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// #2310: `description` is Optional+Computed, so an explicit `description = ""`
// in HCL plans as `""` and the read-back has to hand back that same `""`.
// Collapsing an empty server value to StringNull() makes the apply fail with
// "Provider produced inconsistent result after apply", because a null cannot
// satisfy a planned empty string.
//
// Only the read-back was ever at risk here, unlike the attributes whose SDK
// field needed a pointer. `Description` carries `,omitempty`, so an explicit
// `""` is dropped from the request body -- but the attribute is
// RequiresReplace, so there is no update path and every change is a CREATE. On
// a create a dropped value means the server applies its column default, and
// that default is `""` (the column is NOT NULL, default empty), not some
// previously stored value. So the empty string survives the wire regardless,
// and the read-back is the only place it could be lost.

func TestAnEmptyDescriptionReadsBackAsEmptyNotNull(t *testing.T) {
	for _, tc := range []struct {
		name  string
		token *terrapod.AgentPoolToken
	}{
		// These two are the SAME input at runtime: `Description` is a plain Go
		// string on the SDK struct, so "explicitly empty" and "absent" are both
		// the zero value and the provider cannot tell them apart. Both cases are
		// kept deliberately -- they pin that the read-back stays unconditional
		// rather than growing a guard on a neighbouring field, which is how the
		// collapse would come back. Neither case proves the server distinguishes
		// the two shapes, because at this boundary it cannot.
		{
			name:  "the server answers an explicit empty description",
			token: &terrapod.AgentPoolToken{ID: "apt-c0ffee", Description: ""},
		},
		{
			name:  "the description is absent from the server's answer",
			token: &terrapod.AgentPoolToken{ID: "apt-c0ffee"},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var m agentPoolTokenModel
			readPoolTokenFromSDK(tc.token, &m)

			if m.Description.IsNull() {
				t.Fatal(`description read back as null; a null cannot satisfy an explicit description = "" in config`)
			}
			if m.Description.IsUnknown() {
				t.Fatal("description read back as unknown; a read must produce a concrete value")
			}
			if got := m.Description.ValueString(); got != "" {
				t.Errorf("description = %q, want %q", got, "")
			}
		})
	}
}

func TestANonEmptyDescriptionStillReadsBackVerbatim(t *testing.T) {
	// The empty case is the only thing that changed, so a real description must
	// still arrive intact -- the fix must not be satisfiable by flattening every
	// value to "".
	var m agentPoolTokenModel
	readPoolTokenFromSDK(&terrapod.AgentPoolToken{
		ID:          "apt-c0ffee",
		Description: "CI runners, staging",
	}, &m)

	if got := m.Description.ValueString(); got != "CI runners, staging" {
		t.Errorf("description = %q, want %q", got, "CI runners, staging")
	}
}
