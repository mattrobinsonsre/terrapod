package workspace

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/types"
)

// The two dynamic-inventory switches (#2033) on the workspace DATA SOURCE.
//
// This surface reads attributes off the Resource directly rather than through
// the SDK's workspaceFromResource, so the decoding is restated here -- and
// restated code is code that drifts. These pin the two properties on this
// surface independently of the resource's own tests.
//
// Both columns default FALSE, which REFUSES, so a plain GetBoolAttr would in
// fact answer correctly for an absent key; the case below is here so that
// stays true rather than because it is delicate today. What is delicate is the
// pair being independent: a copy-paste reading one wire attribute into both
// model fields passes every case where the two carry the same value, which is
// why the differing pairs are here.

func TestDataSourceReadsTheDynamicInventorySwitches(t *testing.T) {
	ctx := context.Background()

	for _, tc := range []struct {
		name             string
		attrs            map[string]any
		wantDyn, wantScr bool
	}{
		{"absent takes the columns' own default, which refuses",
			map[string]any{"name": "api"}, false, false},
		{"both permitted",
			map[string]any{
				"name":                      "api",
				"dynamic-inventory-enabled": true,
				"inventory-scripts-enabled": true,
			}, true, true},
		{"both explicitly refused",
			map[string]any{
				"name":                      "api",
				"dynamic-inventory-enabled": false,
				"inventory-scripts-enabled": false,
			}, false, false},
		// The pair exists so declared plugin configs can be permitted without
		// also permitting arbitrary executables, so that asymmetry is the one
		// shape the data source most has to report correctly -- and the only
		// one that catches a single wire attribute read into both fields.
		{"declared configs permitted, executables refused",
			map[string]any{
				"name":                      "api",
				"dynamic-inventory-enabled": true,
				"inventory-scripts-enabled": false,
			}, true, false},
		{"executables permitted, declared configs refused",
			map[string]any{
				"name":                      "api",
				"dynamic-inventory-enabled": false,
				"inventory-scripts-enabled": true,
			}, false, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceDataSourceModel
			if diags := readDataSourceModel(ctx, wsResource(t, tc.attrs), &m); diags.HasError() {
				t.Fatalf("read: %v", diags)
			}
			for _, f := range []struct {
				what string
				got  types.Bool
				want bool
			}{
				{"dynamic_inventory_enabled", m.DynamicInventoryEnabled, tc.wantDyn},
				{"inventory_scripts_enabled", m.InventoryScriptsEnabled, tc.wantScr},
			} {
				if f.got.IsNull() || f.got.IsUnknown() {
					t.Fatalf("%s read back as %v — these are Computed attributes over "+
						"non-nullable columns, so a null is never the right answer",
						f.what, f.got)
				}
				if f.got.ValueBool() != f.want {
					t.Errorf("%s = %v, want %v", f.what, f.got.ValueBool(), f.want)
				}
			}
		})
	}
}
