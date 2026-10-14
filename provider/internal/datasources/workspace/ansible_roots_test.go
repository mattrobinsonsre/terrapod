package workspace

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/types"
)

// The data source reads attributes off the Resource DIRECTLY rather than
// through the SDK's workspaceFromResource, so both of the #1929 rules with a
// trap in them are restated here -- and restated code is code that drifts.
// These pin the two properties on this surface independently of the resource's
// own tests.

func TestDataSourceKeepsConfigDirectoryNullAndEmptyApart(t *testing.T) {
	// `setOptionalString` -- the helper every other string on this data source
	// uses -- maps "" to null. Using it for this attribute is the mistake: a
	// workspace whose configure operation is rooted at the top of its
	// repository would be reported as having no configure operation at all,
	// which is the exact opposite of true and reads as a deliberate answer.
	ctx := context.Background()

	cases := []struct {
		name      string
		attrs     map[string]any
		wantNull  bool
		wantValue string
	}{
		{
			name:     "absent means no configure operation",
			attrs:    map[string]any{"name": "api"},
			wantNull: true,
		},
		{
			name:     "explicit null means no configure operation",
			attrs:    map[string]any{"name": "api", "config-directory": nil},
			wantNull: true,
		},
		{
			name:      "empty means the repository root",
			attrs:     map[string]any{"name": "api", "config-directory": ""},
			wantNull:  false,
			wantValue: "",
		},
		{
			name:      "a value means that subdirectory",
			attrs:     map[string]any{"name": "api", "config-directory": "ansible"},
			wantNull:  false,
			wantValue: "ansible",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceDataSourceModel
			res := wsResource(t, tc.attrs)
			if diags := readDataSourceModel(ctx, res, &m); diags.HasError() {
				t.Fatalf("read: %v", diags)
			}
			if m.ConfigDirectory.IsNull() != tc.wantNull {
				t.Fatalf("config_directory null = %v, want %v (value %q)",
					m.ConfigDirectory.IsNull(), tc.wantNull, m.ConfigDirectory.ValueString())
			}
			if !tc.wantNull && m.ConfigDirectory.ValueString() != tc.wantValue {
				t.Errorf("config_directory = %q, want %q", m.ConfigDirectory.ValueString(), tc.wantValue)
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
	// default stays out of a consumer's comparisons.
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
