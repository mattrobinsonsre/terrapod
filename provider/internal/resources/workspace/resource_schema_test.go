package workspace

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
)

// TestListAttributesAreComputed is the regression guard for #684.
//
// A `terraform_workspace` list attribute that the server can hold a value for
// which the config does NOT set (e.g. drift_ignore_rules set out-of-band via the
// bulk-update endpoint, or left in state after being removed from HCL) MUST be
// Optional+Computed. If it's Optional-only, omitting it plans as `null` while the
// read-back returns the server's non-null list — and the plugin framework fails
// the apply with "Provider produced inconsistent result after apply". Keeping
// these Computed makes config-omission mean "leave alone" (plan = unknown).
func TestListAttributesAreComputed(t *testing.T) {
	var resp resource.SchemaResponse
	NewResource().Schema(context.Background(), resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema build error: %v", resp.Diagnostics)
	}

	for _, name := range []string{"drift_ignore_rules", "var_files", "trigger_prefixes"} {
		attr, ok := resp.Schema.Attributes[name]
		if !ok {
			t.Fatalf("attribute %q missing from schema", name)
		}
		if !attr.IsComputed() {
			t.Errorf("attribute %q must be Computed (#684) to tolerate a server-held value the config omits", name)
		}
		if !attr.IsOptional() {
			t.Errorf("attribute %q must stay Optional", name)
		}
	}
}
