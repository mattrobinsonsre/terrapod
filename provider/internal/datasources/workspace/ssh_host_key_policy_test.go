package workspace

import (
	"context"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
)

// `ssh_host_key_policy` (#2592) on the workspace DATA SOURCE.
//
// This surface reads attributes off the Resource directly rather than through
// the SDK's workspaceFromResource, so the decoding is restated here -- and
// restated code is code that drifts. Hence a test on this surface
// independently of the resource's own.
//
// The absent-key case is the delicate one, and it is delicate in the same way
// the boolean's was before #2592 renamed it, with only the dangerous VALUE
// changed. A plain GetStringAttr answers "" for a key that is not there, and ""
// is not a policy -- so on a workspace from a server predating this attribute,
// or one read through a surface that does not serialise it, a plain read would
// report a policy the three-value set does not contain. Falling back to
// "strict" is the safe direction for the same reason the boolean fell back to
// TRUE: the mistake must never be to report a workspace as verifying LESS than
// it does.
func TestDataSourceReadsTheHostKeyPolicyWithItsOwnDefault(t *testing.T) {
	ctx := context.Background()

	for _, tc := range []struct {
		name  string
		attrs map[string]any
		want  string
	}{
		// The case a plain GetStringAttr fails, and it fails by reporting a
		// strictly-verifying workspace as having no policy at all.
		{"absent takes the column's own strict default",
			map[string]any{"name": "api"}, "strict"},
		{"explicitly strict",
			map[string]any{"name": "api", "ssh-host-key-policy": "strict"}, "strict"},
		// The state the boolean could not hold. A decode that collapsed the
		// policy to a boolean would fail here and nowhere else.
		{"accept-new",
			map[string]any{"name": "api", "ssh-host-key-policy": "accept-new"}, "accept-new"},
		// And this is the case a read hardcoded to the default would fail, so
		// the set together pins the decode rather than either default.
		{"explicitly off",
			map[string]any{"name": "api", "ssh-host-key-policy": "off"}, "off"},
		// A JSON null is not the same as an absent key on the wire, and
		// GetStringAttr answers "" for both -- so the default must apply here
		// too rather than the attribute reading as empty or as null.
		{"an explicit null still takes the default",
			map[string]any{"name": "api", "ssh-host-key-policy": nil}, "strict"},
		// An explicit empty string is the same case by a third route. The
		// server cannot store it, so reading it back as a policy would be
		// reporting something that does not exist.
		{"an explicit empty string still takes the default",
			map[string]any{"name": "api", "ssh-host-key-policy": ""}, "strict"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceDataSourceModel
			if diags := readDataSourceModel(ctx, wsResource(t, tc.attrs), &m); diags.HasError() {
				t.Fatalf("read: %v", diags)
			}
			got := m.SSHHostKeyPolicy
			if got.IsNull() || got.IsUnknown() {
				t.Fatalf("ssh_host_key_policy read back as %v, want a concrete value", got)
			}
			if got.ValueString() != tc.want {
				t.Errorf("ssh_host_key_policy = %q, want %q", got.ValueString(), tc.want)
			}
		})
	}
}

func TestDataSourceHostKeyPolicyIsComputedOnly(t *testing.T) {
	// A data source reports; it does not set. Optional here would invite a
	// configuration to "ask for" a value the surface cannot write -- and would
	// also raise the question of validating it, which a read has no business
	// doing: the server's answer is the answer.
	var resp datasource.SchemaResponse
	NewDataSource().Schema(context.Background(), datasource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema build error: %v", resp.Diagnostics)
	}

	const name = "ssh_host_key_policy"
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

	// The rename's other half: a data source offering both would report a
	// boolean no surface of the API still answers.
	for n := range resp.Schema.Attributes {
		if strings.Contains(n, "host_key_checking") {
			t.Errorf("attribute %q is still reported: #2592 replaced it with "+
				"ssh_host_key_policy", n)
		}
	}
}
