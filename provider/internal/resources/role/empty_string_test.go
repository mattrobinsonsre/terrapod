package role

import (
	"context"
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
		role *terrapod.Role
	}{
		{
			name: "the server answers an explicit empty description",
			role: &terrapod.Role{Name: "deployer", WorkspacePermission: "write", Description: ""},
		},
		{
			name: "the description is absent from the server's answer",
			role: &terrapod.Role{Name: "deployer", WorkspacePermission: "write"},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var m roleModel
			if diags := readRoleFromSDK(context.Background(), tc.role, &m); diags.HasError() {
				t.Fatalf("reading the role back: %v", diags)
			}

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
	var m roleModel
	role := &terrapod.Role{Name: "deployer", WorkspacePermission: "write", Description: "may apply in staging"}
	if diags := readRoleFromSDK(context.Background(), role, &m); diags.HasError() {
		t.Fatalf("reading the role back: %v", diags)
	}

	if got := m.Description.ValueString(); got != "may apply in staging" {
		t.Errorf("description = %q, want %q", got, "may apply in staging")
	}
}
