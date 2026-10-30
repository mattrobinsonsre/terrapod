package variable_set_variable

import (
	"testing"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// #2310: `description` is Optional+Computed, so an explicit `description = ""`
// in HCL plans as `""` and the read-back has to hand back that same `""`.
// Collapsing an empty server value to StringNull() -- which this read-back used
// to do -- makes the apply fail with "Provider produced inconsistent result
// after apply", because a null cannot satisfy a planned empty string.
//
// The zero-valued case is the same input by construction, since the SDK field
// is a plain `string`. It is asserted separately on purpose: it pins that the
// read-back stays unconditional rather than growing a guard on some other
// field, which is how the collapse would come back.

func TestAnEmptyDescriptionReadsBackAsEmptyNotNull(t *testing.T) {
	for _, tc := range []struct {
		name string
		v    *terrapod.VariableSetVariable
	}{
		{
			name: "the server answers an explicit empty description",
			v:    &terrapod.VariableSetVariable{ID: "var-1", Key: "region", Category: "terraform", Description: ""},
		},
		{
			name: "the description is absent from the server's answer",
			v:    &terrapod.VariableSetVariable{ID: "var-1", Key: "region", Category: "terraform"},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var m variableSetVariableModel
			readVSVFromSDK(tc.v, &m)

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
	// The empty case is the only thing that changed -- a real description must
	// still arrive intact.
	var m variableSetVariableModel
	readVSVFromSDK(&terrapod.VariableSetVariable{
		ID: "var-1", Key: "region", Category: "terraform", Description: "the deployment region",
	}, &m)

	if got := m.Description.ValueString(); got != "the deployment region" {
		t.Errorf("description = %q, want %q", got, "the deployment region")
	}
}
