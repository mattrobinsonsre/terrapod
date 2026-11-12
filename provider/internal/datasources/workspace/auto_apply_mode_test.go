package workspace

import (
	"context"
	"testing"
)

// `auto_apply_mode` on the workspace DATA SOURCE (#2503).
//
// This is the surface the issue's scope record singled out, and it was wrong
// in BOTH directions at once -- neither of which is an error a practitioner
// could see:
//
//   - `auto_apply` was read with GetBoolAttr. That compiles against a server
//     that no longer emits the key and answers a confident `false` for every
//     workspace, which is the dangerous direction for an automation setting:
//     a workspace that applies unattended reads as one that does not.
//   - `auto_apply_mode` was DECLARED in the model and the schema and never
//     assigned, so the attribute the boolean was being replaced by reported a
//     blank string on every read. Removing the boolean without fixing this
//     would have left the data source reporting nothing at all.
//
// Hence a read test rather than a schema one: the schema gate next door
// (internal/provider) says the boolean is gone, and only driving the decode
// says the replacement actually carries a value.
func TestDataSourceReadsAutoApplyMode(t *testing.T) {
	ctx := context.Background()

	for _, tc := range []struct {
		name  string
		attrs map[string]any
		want  string
	}{
		{"never", map[string]any{"name": "api", "auto-apply-mode": "never"}, "never"},
		{"always", map[string]any{"name": "api", "auto-apply-mode": "always"}, "always"},
		// The conditional modes are the whole reason the boolean could not
		// stay: neither is expressible as one, so a reader given only a
		// boolean cannot tell a guarded workspace from an unguarded one.
		{"create", map[string]any{"name": "api", "auto-apply-mode": "create"}, "create"},
		{"create_update",
			map[string]any{"name": "api", "auto-apply-mode": "create_update"}, "create_update"},
		// A server that does not serialise the key -- /api/tfe/v2 -- leaves an
		// empty string rather than an unset attribute, so a consumer reads a
		// value it can test rather than a null it has to special-case.
		{"absent reads empty", map[string]any{"name": "api"}, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceDataSourceModel
			if diags := readDataSourceModel(ctx, wsResource(t, tc.attrs), &m); diags.HasError() {
				t.Fatalf("read: %v", diags)
			}
			if m.AutoApplyMode.IsNull() || m.AutoApplyMode.IsUnknown() {
				t.Fatalf("auto_apply_mode read back as %v, want a concrete value -- this is "+
					"the defect: the attribute was declared and never assigned", m.AutoApplyMode)
			}
			if got := m.AutoApplyMode.ValueString(); got != tc.want {
				t.Errorf("auto_apply_mode = %q, want %q", got, tc.want)
			}
		})
	}
}
