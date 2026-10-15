package workspace

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/types"
)

// A workspace's operations (#1966) and the ansible configure operation's roots
// and run defaults (#1929), on the data source.
//
// The data source reads attributes off the Resource DIRECTLY rather than
// through the SDK's workspaceFromResource, so every rule with a trap in it is
// restated here -- and restated code is code that drifts. These pin the
// properties on this surface independently of the resource's own tests.
//
// This file used to open with TestDataSourceKeepsConfigDirectoryNullAndEmptyApart,
// and its premise is what #1966 changed: null was a meaningful third state
// meaning "this workspace has no configure operation". `configure_enabled`
// answers that now, the column is non-nullable, and `config_directory` is an
// ordinary path whose empty value is the configuration version's root. So the
// property is the opposite one -- "" must NOT come back as null -- and the two
// booleans bring a trap of their own.

func TestDataSourceReadsConfigDirectoryAsAPlainNonNullablePath(t *testing.T) {
	// `setOptionalString` -- the helper every other string on this data source
	// uses -- maps "" to null, and "" is a real value here: the configuration
	// version's root. Using it would report every root-rooted workspace as
	// having no path at all, which reads as a deliberate answer rather than a
	// decoding bug. Absent reads as "" too, because that is the column's own
	// default and a server predating #1929 sends nothing.
	ctx := context.Background()

	for _, tc := range []struct {
		name  string
		attrs map[string]any
		want  string
	}{
		{"absent is the column default", map[string]any{"name": "api"}, ""},
		{"empty is the configuration version's root",
			map[string]any{"name": "api", "config-directory": ""}, ""},
		{"a value is that subdirectory",
			map[string]any{"name": "api", "config-directory": "ansible"}, "ansible"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceDataSourceModel
			if diags := readDataSourceModel(ctx, wsResource(t, tc.attrs), &m); diags.HasError() {
				t.Fatalf("read: %v", diags)
			}
			if m.ConfigDirectory.IsNull() || m.ConfigDirectory.IsUnknown() {
				t.Fatalf("config_directory read back as %v — it is a Computed attribute "+
					"on a non-nullable column, so a null is never the right answer",
					m.ConfigDirectory)
			}
			if got := m.ConfigDirectory.ValueString(); got != tc.want {
				t.Errorf("config_directory = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestDataSourceReadsTheOperationsWithTheirOwnDefaults(t *testing.T) {
	// The defaults differ between the two, which is the whole reason a plain
	// GetBoolAttr is wrong here. It answers FALSE for an absent key, and
	// `plan-apply-enabled` defaults TRUE -- so on every workspace of a server
	// predating #1966, and on every workspace read through /api/tfe/v2 (which
	// serialises neither), an ordinary terraform workspace would be reported
	// as having no plan/apply operation. That is a workspace that can never
	// run, reported as a deliberate configuration.
	ctx := context.Background()

	for _, tc := range []struct {
		name          string
		attrs         map[string]any
		wantPlanApply bool
		wantConfigure bool
	}{
		{"absent takes each column's own default",
			map[string]any{"name": "api"}, true, false},
		{"configure-only",
			map[string]any{"name": "api", "plan-apply-enabled": false, "configure-enabled": true},
			false, true},
		{"both on",
			map[string]any{"name": "api", "plan-apply-enabled": true, "configure-enabled": true},
			true, true},
		{"an explicit false on each stays false",
			// `configure-enabled` false matches its default, so this case
			// cannot catch a dropped read on its own; it is here so that an
			// explicit value is asserted on both attributes in both
			// directions.
			map[string]any{"name": "api", "plan-apply-enabled": true, "configure-enabled": false},
			true, false},
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
				{"plan_apply_enabled", m.PlanApplyEnabled, tc.wantPlanApply},
				{"configure_enabled", m.ConfigureEnabled, tc.wantConfigure},
			} {
				if f.got.IsNull() || f.got.IsUnknown() {
					t.Fatalf("%s read back as %v, want a concrete value", f.what, f.got)
				}
				if f.got.ValueBool() != f.want {
					t.Errorf("%s = %v, want %v", f.what, f.got.ValueBool(), f.want)
				}
			}
		})
	}
}

func TestDataSourceReadsIncludePlatformAbsentAsTrue(t *testing.T) {
	// The column defaults to TRUE, so a plain GetBoolAttr -- which answers
	// false for an absent key -- reports the declared rows as excluded on
	// every workspace of a server predating #1929, and on every workspace read
	// through /api/tfe/v2, which drops all six attributes. Both directions are
	// asserted: absent must be true, and an explicit false must stay false.
	ctx := context.Background()

	cases := []struct {
		name  string
		attrs map[string]any
		want  bool
	}{
		{"absent is true", map[string]any{"name": "api"}, true},
		{"explicit true", map[string]any{"name": "api", "inventory-include-platform": true}, true},
		{"explicit false", map[string]any{"name": "api", "inventory-include-platform": false}, false},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceDataSourceModel
			res := wsResource(t, tc.attrs)
			if diags := readDataSourceModel(ctx, res, &m); diags.HasError() {
				t.Fatalf("read: %v", diags)
			}
			if m.InventoryIncludePlatform.IsNull() || m.InventoryIncludePlatform.IsUnknown() {
				t.Fatalf("inventory_include_platform read back as %v, want a concrete value",
					m.InventoryIncludePlatform)
			}
			if got := m.InventoryIncludePlatform.ValueBool(); got != tc.want {
				t.Errorf("inventory_include_platform = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestDataSourceReadsTheRemainingRoots(t *testing.T) {
	ctx := context.Background()

	var m workspaceDataSourceModel
	res := wsResource(t, map[string]any{
		"name":                   "configure-only",
		"inventory-paths":        []string{"inventory/production", "inventory/extra.yml"},
		"default-playbook":       "site.yml",
		"default-limit":          "web",
		"inventory-ignore-paths": []string{"archive/"},
	})
	if diags := readDataSourceModel(ctx, res, &m); diags.HasError() {
		t.Fatalf("read: %v", diags)
	}

	els := m.InventoryPaths.Elements()
	if len(els) != 2 || els[0].(types.String).ValueString() != "inventory/production" {
		t.Errorf("inventory_paths = %v — the order IS the precedence ansible applies", els)
	}
	if m.DefaultPlaybook.ValueString() != "site.yml" {
		t.Errorf("default_playbook = %q", m.DefaultPlaybook.ValueString())
	}
	if m.DefaultLimit.ValueString() != "web" {
		t.Errorf("default_limit = %q", m.DefaultLimit.ValueString())
	}
	ig := m.InventoryIgnorePaths.Elements()
	if len(ig) != 1 || ig[0].(types.String).ValueString() != "archive/" {
		t.Errorf("inventory_ignore_paths = %v", ig)
	}

	// Empty reads as null for the two strings, so a workspace declaring no
	// default stays out of a consumer's comparisons. `config_directory` is
	// deliberately NOT in this set any more -- see its own test above.
	var m2 workspaceDataSourceModel
	if diags := readDataSourceModel(ctx, wsResource(t, map[string]any{"name": "api"}), &m2); diags.HasError() {
		t.Fatalf("read: %v", diags)
	}
	if !m2.DefaultPlaybook.IsNull() || !m2.DefaultLimit.IsNull() {
		t.Errorf("absent defaults read back as playbook=%v limit=%v, want null",
			m2.DefaultPlaybook, m2.DefaultLimit)
	}
	if !m2.InventoryPaths.IsNull() || !m2.InventoryIgnorePaths.IsNull() {
		t.Errorf("absent lists read back as paths=%v ignore=%v, want null",
			m2.InventoryPaths, m2.InventoryIgnorePaths)
	}
}
