package workspace

import (
	"context"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
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

// TestUnmanagedCollectionDescriptionsDocumentClearing is the regression guard
// for #1091.
//
// Because these attributes are Optional+Computed (#684 above), *removing* one
// from a config is a silent no-op: the plan reports no changes and the workspace
// keeps its value. That convention only ever existed in a source comment, so the
// natural way to remove a var-file — deleting the line — failed silently. The
// rendered Description is what a practitioner actually reads, so it has to say
// so.
func TestUnmanagedCollectionDescriptionsDocumentClearing(t *testing.T) {
	var resp resource.SchemaResponse
	NewResource().Schema(context.Background(), resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema build error: %v", resp.Diagnostics)
	}

	for _, a := range unmanagedCollections {
		attr, ok := resp.Schema.Attributes[a.name]
		if !ok {
			t.Fatalf("attribute %q missing from schema", a.name)
		}
		desc := attr.GetDescription()
		if !strings.Contains(desc, a.name+" = []") {
			t.Errorf("attribute %q description must tell the reader to set `%s = []` to clear (#1091); got: %s",
				a.name, a.name, desc)
		}
		if !strings.Contains(desc, "does not clear it") {
			t.Errorf("attribute %q description must state that omitting it does NOT clear the value (#1091); got: %s",
				a.name, desc)
		}
	}
}

// TestUnmanagedCollectionsCoversEveryOptionalComputedCollection stops a future
// Optional+Computed list attribute from being added with the same silent-no-op
// removal behaviour and no warning or documentation (#1091).
func TestUnmanagedCollectionsCoversEveryOptionalComputedCollection(t *testing.T) {
	var resp resource.SchemaResponse
	NewResource().Schema(context.Background(), resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema build error: %v", resp.Diagnostics)
	}

	covered := map[string]bool{}
	for _, a := range unmanagedCollections {
		covered[a.name] = true
	}

	for name, attr := range resp.Schema.Attributes {
		if _, isList := attr.(schema.ListAttribute); !isList {
			continue
		}
		if !attr.IsOptional() || !attr.IsComputed() {
			continue
		}
		if !covered[name] {
			t.Errorf("list attribute %q is Optional+Computed but is not in unmanagedCollections — "+
				"removing it from a config would be a silent no-op with no warning and no "+
				"documentation (#1091). Add it, or make it Optional-only.", name)
		}
	}
}
