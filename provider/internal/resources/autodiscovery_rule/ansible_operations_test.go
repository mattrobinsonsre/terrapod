package autodiscovery_rule

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// Which operations a materialised workspace has (#1966) and the roots each one
// uses (#1929), templated onto every workspace this rule creates.
//
// The rule carried NONE of the six #1929 columns until now -- a gap left when
// those became workspace columns -- so a Terraform-declared rule could not
// materialise a configure-capable workspace at all, however the workspaces it
// created were then edited by hand. The eight attributes here close that and
// add #1966's two.
//
// The server already accepts and serialises all eight on
// POST/PATCH /api/v1/autodiscovery-rules; it membership-tests each key
// (`if "config-directory" in attrs`) rather than testing truthiness, which is
// what the empty-value cases below depend on.

func TestTheEightAnsibleAttributesAreAllOnTheSchema(t *testing.T) {
	// The gap this change closes, asserted as a gap: before it, every one of
	// the six #1929 names was missing from this resource entirely, so a
	// practitioner's `config_directory = "ansible"` in a rule block was an
	// unknown-attribute error.
	var resp resource.SchemaResponse
	NewResource().Schema(context.Background(), resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema build error: %v", resp.Diagnostics)
	}

	for _, name := range []string{
		"plan_apply_enabled", "configure_enabled", "config_directory",
		"inventory_paths", "default_playbook", "default_limit",
		"inventory_include_platform", "inventory_ignore_paths",
	} {
		a, ok := resp.Schema.Attributes[name]
		if !ok {
			t.Errorf("%s is missing from the rule schema — a rule cannot template it "+
				"onto the workspaces it creates", name)
			continue
		}
		if !a.IsOptional() {
			t.Errorf("%s must be Optional", name)
		}
		// Optional+Computed throughout, the convention for every templated
		// workspace setting on this resource: the server answers with a
		// concrete value for all eight, so Optional alone would plan null
		// against it and fail the apply of any rule that omits the attribute.
		if !a.IsComputed() {
			t.Errorf("%s must be Computed: the server supplies a default the provider "+
				"does not, so an Optional-only attribute plans null against a concrete "+
				"server answer and fails every apply that omits it", name)
		}
	}
}

func TestTheOperationsReachTheWireIncludingAFalse(t *testing.T) {
	// `plan-apply-enabled` defaults TRUE on the server, so FALSE is the
	// direction that matters: a truthiness guard in the attrs builder would
	// drop it and a rule declaring configure-only workspaces would silently
	// go on materialising plan/apply ones.
	for _, tc := range []struct {
		name          string
		planApply     types.Bool
		configure     types.Bool
		wantPlanApply any // nil means the key must be absent
		wantConfigure any
	}{
		{"configure-only", types.BoolValue(false), types.BoolValue(true), false, true},
		{"both on", types.BoolValue(true), types.BoolValue(true), true, true},
		{"configure explicitly off", types.BoolValue(true), types.BoolValue(false), true, false},
		{"omitted leaves the server's values alone",
			types.BoolNull(), types.BoolNull(), nil, nil},
		{"unknown is not a declaration",
			types.BoolUnknown(), types.BoolUnknown(), nil, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			attrs := buildAutodiscoveryRuleAttrs(&autodiscoveryRuleModel{
				PlanApplyEnabled: tc.planApply,
				ConfigureEnabled: tc.configure,
			})
			assertAttr(t, attrs, "plan-apply-enabled", tc.wantPlanApply)
			assertAttr(t, attrs, "configure-enabled", tc.wantConfigure)
		})
	}
}

func TestTheThreeAnsibleStringsTravelEvenWhenEmpty(t *testing.T) {
	// The server membership-tests these keys, and "" is a real value on each:
	// the configuration version's root, no default playbook, the whole
	// inventory. A truthiness guard in the attrs builder would leave a
	// practitioner no way to say any of the three.
	t.Run("empty values are sent", func(t *testing.T) {
		attrs := buildAutodiscoveryRuleAttrs(&autodiscoveryRuleModel{
			ConfigDirectory: types.StringValue(""),
			DefaultPlaybook: types.StringValue(""),
			DefaultLimit:    types.StringValue(""),
		})
		assertAttr(t, attrs, "config-directory", "")
		assertAttr(t, attrs, "default-playbook", "")
		assertAttr(t, attrs, "default-limit", "")
	})

	t.Run("real values are sent", func(t *testing.T) {
		attrs := buildAutodiscoveryRuleAttrs(&autodiscoveryRuleModel{
			ConfigDirectory: types.StringValue("ansible"),
			DefaultPlaybook: types.StringValue("site.yml"),
			DefaultLimit:    types.StringValue("web"),
		})
		assertAttr(t, attrs, "config-directory", "ansible")
		assertAttr(t, attrs, "default-playbook", "site.yml")
		assertAttr(t, attrs, "default-limit", "web")
	})

	t.Run("omitted leaves the server's values alone", func(t *testing.T) {
		attrs := buildAutodiscoveryRuleAttrs(&autodiscoveryRuleModel{
			ConfigDirectory: types.StringNull(),
			DefaultPlaybook: types.StringUnknown(),
			DefaultLimit:    types.StringNull(),
		})
		assertAttr(t, attrs, "config-directory", nil)
		assertAttr(t, attrs, "default-playbook", nil)
		assertAttr(t, attrs, "default-limit", nil)
	})
}

func TestTheInventoryListsKeepTheirOrderAndAnEmptyListClears(t *testing.T) {
	// Order is precedence for ansible, so a reordering is a behaviour change
	// and neither list may be treated as a set anywhere along the path.
	attrs := buildAutodiscoveryRuleAttrs(&autodiscoveryRuleModel{
		InventoryPaths: types.ListValueMust(types.StringType, []attr.Value{
			types.StringValue("inventory/production"),
			types.StringValue("inventory/extra.yml"),
		}),
		InventoryIgnorePaths: types.ListValueMust(types.StringType, []attr.Value{}),
	})

	got, ok := attrs["inventory-paths"].([]string)
	if !ok {
		t.Fatalf("inventory-paths is %T, want []string", attrs["inventory-paths"])
	}
	if len(got) != 2 || got[0] != "inventory/production" || got[1] != "inventory/extra.yml" {
		t.Errorf("inventory-paths = %v — the order IS the precedence ansible applies", got)
	}

	// An explicit empty list is a real request: it removes every entry. It has
	// to arrive as a non-nil `[]string` so it marshals as `[]` rather than
	// being dropped.
	ignore, ok := attrs["inventory-ignore-paths"].([]string)
	if !ok {
		t.Fatalf("an explicit empty inventory_ignore_paths is %T, want []string — the one "+
			"config that clears it would silently do nothing", attrs["inventory-ignore-paths"])
	}
	if len(ignore) != 0 {
		t.Errorf("inventory-ignore-paths = %v, want empty", ignore)
	}

	// Omitted means omitted, so a rule that does not manage the attribute
	// leaves whatever the server holds.
	attrs = buildAutodiscoveryRuleAttrs(&autodiscoveryRuleModel{
		InventoryPaths:       types.ListNull(types.StringType),
		InventoryIgnorePaths: types.ListUnknown(types.StringType),
	})
	assertAttr(t, attrs, "inventory-paths", nil)
	assertAttr(t, attrs, "inventory-ignore-paths", nil)
}

func TestTheRuleReadsTheOperationsWithTheirOwnDefaults(t *testing.T) {
	// Each boolean falls back to its own column default when the attribute is
	// ABSENT, not to Go's zero value. `plan-apply-enabled` defaults TRUE, so a
	// plain GetBoolAttr would read a rule on a server predating #1966 as
	// materialising workspaces that can never run -- and then plan a change to
	// "fix" a rule nobody had altered.
	ctx := context.Background()

	for _, tc := range []struct {
		name          string
		attrs         map[string]any
		wantPlanApply bool
		wantConfigure bool
		wantInclude   bool
	}{
		{"absent takes each column's own default",
			map[string]any{"name-template": "x"}, true, false, true},
		{"configure-only", map[string]any{
			"name-template":              "x",
			"plan-apply-enabled":         false,
			"configure-enabled":          true,
			"inventory-include-platform": false,
		}, false, true, false},
		{"all three explicitly on", map[string]any{
			"name-template":              "x",
			"plan-apply-enabled":         true,
			"configure-enabled":          true,
			"inventory-include-platform": true,
		}, true, true, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			m := &autodiscoveryRuleModel{}
			if d := readAutodiscoveryRuleIntoModel(ctx, ruleResource(t, tc.attrs), m); d.HasError() {
				t.Fatalf("read: %v", d)
			}
			for _, f := range []struct {
				what string
				got  types.Bool
				want bool
			}{
				{"plan_apply_enabled", m.PlanApplyEnabled, tc.wantPlanApply},
				{"configure_enabled", m.ConfigureEnabled, tc.wantConfigure},
				{"inventory_include_platform", m.InventoryIncludePlatform, tc.wantInclude},
			} {
				if f.got.IsNull() || f.got.IsUnknown() {
					t.Fatalf("%s read back as %v, want a concrete value — all three are "+
						"Computed, so a null read-back is the inconsistent-result shape",
						f.what, f.got)
				}
				if f.got.ValueBool() != f.want {
					t.Errorf("%s = %v, want %v", f.what, f.got.ValueBool(), f.want)
				}
			}
		})
	}
}

func TestTheRuleReadsTheAnsibleRootsAsPlainNonNullableValues(t *testing.T) {
	// All three strings are non-nullable columns whose empty value is
	// meaningful, so the read is an unconditional projection. Mapping "" to
	// null -- which is what the `if v != "" else null` pattern elsewhere in
	// this file does -- would report every rule rooted at the configuration
	// version's root as having no path, against a plan of "".
	ctx := context.Background()

	for _, tc := range []struct {
		name                             string
		attrs                            map[string]any
		wantDir, wantPlaybook, wantLimit string
	}{
		{"absent takes the column defaults",
			map[string]any{"name-template": "x"}, "", "", ""},
		{"explicit empties round-trip as empties", map[string]any{
			"name-template":    "x",
			"config-directory": "", "default-playbook": "", "default-limit": "",
		}, "", "", ""},
		{"values round-trip", map[string]any{
			"name-template":    "x",
			"config-directory": "ansible", "default-playbook": "site.yml", "default-limit": "web",
		}, "ansible", "site.yml", "web"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			m := &autodiscoveryRuleModel{}
			if d := readAutodiscoveryRuleIntoModel(ctx, ruleResource(t, tc.attrs), m); d.HasError() {
				t.Fatalf("read: %v", d)
			}
			for _, f := range []struct {
				what string
				got  types.String
				want string
			}{
				{"config_directory", m.ConfigDirectory, tc.wantDir},
				{"default_playbook", m.DefaultPlaybook, tc.wantPlaybook},
				{"default_limit", m.DefaultLimit, tc.wantLimit},
			} {
				if f.got.IsNull() || f.got.IsUnknown() {
					t.Fatalf("%s read back as %v, want a concrete value", f.what, f.got)
				}
				if f.got.ValueString() != f.want {
					t.Errorf("%s = %q, want %q", f.what, f.got.ValueString(), f.want)
				}
			}
		})
	}
}

func TestTheRuleReadPreservesAPriorNullListButNotAPriorEmptyOne(t *testing.T) {
	// Both halves of the null-versus-empty rule, and they pull in opposite
	// directions -- which is why both are asserted. A prior NULL plus an
	// empty server answer must stay null, or a rule that never declared the
	// attribute plans a diff against `[]` on every run. A prior `[]` plus an
	// empty server answer must stay `[]`, or the apply of a rule that writes
	// `inventory_paths = []` explicitly fails on the read-back.
	ctx := context.Background()

	t.Run("a prior null survives an empty server answer", func(t *testing.T) {
		m := &autodiscoveryRuleModel{
			InventoryPaths:       types.ListNull(types.StringType),
			InventoryIgnorePaths: types.ListNull(types.StringType),
		}
		if d := readAutodiscoveryRuleIntoModel(ctx, ruleResource(t, map[string]any{
			"name-template": "x",
		}), m); d.HasError() {
			t.Fatalf("read: %v", d)
		}
		if !m.InventoryPaths.IsNull() {
			t.Errorf("inventory_paths materialised as %v from a prior null and an "+
				"empty server value", m.InventoryPaths)
		}
		if !m.InventoryIgnorePaths.IsNull() {
			t.Errorf("inventory_ignore_paths materialised as %v", m.InventoryIgnorePaths)
		}
	})

	t.Run("a prior empty list stays an empty list", func(t *testing.T) {
		empty := types.ListValueMust(types.StringType, []attr.Value{})
		m := &autodiscoveryRuleModel{InventoryPaths: empty, InventoryIgnorePaths: empty}
		// The server echoes `[]`, and separately an older server sends
		// nothing at all; neither may come back as null against a plan of [].
		for _, attrs := range []map[string]any{
			{"name-template": "x", "inventory-paths": []string{}, "inventory-ignore-paths": []string{}},
			{"name-template": "x"},
		} {
			got := &autodiscoveryRuleModel{
				InventoryPaths:       m.InventoryPaths,
				InventoryIgnorePaths: m.InventoryIgnorePaths,
			}
			if d := readAutodiscoveryRuleIntoModel(ctx, ruleResource(t, attrs), got); d.HasError() {
				t.Fatalf("read: %v", d)
			}
			if got.InventoryPaths.IsNull() || got.InventoryIgnorePaths.IsNull() {
				t.Errorf("an explicit empty list read back as null from %v — that is "+
					"\"Provider produced inconsistent result after apply\"", attrs)
			}
		}
	})

	t.Run("values read back in order", func(t *testing.T) {
		m := &autodiscoveryRuleModel{InventoryPaths: types.ListNull(types.StringType)}
		if d := readAutodiscoveryRuleIntoModel(ctx, ruleResource(t, map[string]any{
			"name-template":   "x",
			"inventory-paths": []string{"inventory/production", "inventory/extra.yml"},
		}), m); d.HasError() {
			t.Fatalf("read: %v", d)
		}
		els := m.InventoryPaths.Elements()
		if len(els) != 2 || els[0].(types.String).ValueString() != "inventory/production" {
			t.Errorf("inventory_paths = %v — order is precedence", els)
		}
	})
}

// assertAttr checks one key of the attrs map: `want == nil` means the key must
// be ABSENT, which is a different assertion from it being present and empty.
func assertAttr(t *testing.T, attrs map[string]any, key string, want any) {
	t.Helper()
	got, present := attrs[key]
	if want == nil {
		if present {
			t.Errorf("%s = %#v, want the key absent so the server's value survives", key, got)
		}
		return
	}
	if !present {
		t.Errorf("%s is absent, want %#v — the declaration never reaches the server", key, want)
		return
	}
	if got != want {
		t.Errorf("%s = %#v, want %#v", key, got, want)
	}
}
