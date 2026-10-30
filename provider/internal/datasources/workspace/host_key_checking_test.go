package workspace

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
)

// `ansible_host_key_checking` (#2318) on the workspace DATA SOURCE.
//
// This surface reads attributes off the Resource directly rather than through
// the SDK's workspaceFromResource, so the decoding is restated here -- and
// restated code is code that drifts. Hence a test on this surface
// independently of the resource's own.
//
// The absent-key case is the delicate one, and it is delicate in a way its
// false-defaulting neighbours are not. A plain GetBoolAttr answers FALSE for a
// key that is not there, and this column defaults TRUE -- so on a workspace
// from a server predating #2318, and on one read through /api/tfe/v2 (which
// does not serialise it), a plain read would report a workspace as NOT
// verifying host keys when it does. That is the same wrong-in-the-dangerous-
// direction shape as `plan-apply-enabled` next door, and the reason both fall
// back to their own default at the read site rather than to Go's zero value.
func TestDataSourceReadsHostKeyCheckingWithItsOwnDefault(t *testing.T) {
	ctx := context.Background()

	for _, tc := range []struct {
		name  string
		attrs map[string]any
		want  bool
	}{
		// The case a plain GetBoolAttr fails, and it fails by reporting a
		// verifying workspace as relaxed.
		{"absent takes the column's own verifying default",
			map[string]any{"name": "api"}, true},
		{"explicitly verifying",
			map[string]any{"name": "api", "ansible-host-key-checking": true}, true},
		// And this is the case a read-back hardcoded to the default would fail,
		// so the two together pin the decode rather than either default.
		{"explicitly relaxed",
			map[string]any{"name": "api", "ansible-host-key-checking": false}, false},
		// A JSON null is not the same as an absent key on the wire, and
		// GetBoolPtrAttr treats both as "the server did not say" -- so the
		// default must apply here too rather than the attribute reading as
		// relaxed or as null.
		{"an explicit null still takes the default",
			map[string]any{"name": "api", "ansible-host-key-checking": nil}, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceDataSourceModel
			if diags := readDataSourceModel(ctx, wsResource(t, tc.attrs), &m); diags.HasError() {
				t.Fatalf("read: %v", diags)
			}
			got := m.AnsibleHostKeyChecking
			if got.IsNull() || got.IsUnknown() {
				t.Fatalf("ansible_host_key_checking read back as %v, want a concrete value", got)
			}
			if got.ValueBool() != tc.want {
				t.Errorf("ansible_host_key_checking = %v, want %v", got.ValueBool(), tc.want)
			}
		})
	}
}

func TestDataSourceHostKeyCheckingIsComputedOnly(t *testing.T) {
	// A data source reports; it does not set. Optional here would invite a
	// configuration to "ask for" a value the surface cannot write.
	var resp datasource.SchemaResponse
	NewDataSource().Schema(context.Background(), datasource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema build error: %v", resp.Diagnostics)
	}

	const name = "ansible_host_key_checking"
	attr, ok := resp.Schema.Attributes[name]
	if !ok {
		t.Fatalf("attribute %q is missing from the terrapod_workspace data source", name)
	}
	if !attr.IsComputed() {
		t.Errorf("attribute %q must be Computed on the data source", name)
	}
	if attr.IsOptional() || attr.IsRequired() {
		t.Errorf("attribute %q must be neither Optional nor Required on a data source "+
			"— it is read from the workspace, never supplied", name)
	}
}
