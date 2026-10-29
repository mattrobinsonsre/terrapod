package catalog_item

import (
	"context"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/tfsdk"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-go/tftypes"
	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// The Ansible half a catalog item provisions (#2019): `configure_enabled`,
// `ansible_directory`, `default_playbook`, `default_limit`.
//
// An item may provision a workspace that is primarily Ansible, carrying only as
// much Terraform as it takes to declare the inventory. These four are templated
// onto the provisioned workspace, so the item is where an operator declares
// them once rather than editing every workspace the catalog creates.
//
// The property every test here circles is that the provider NORMALISES NOTHING.
// The server stores these byte for byte and refuses what it will not take; the
// provider writes the server's answer back into state, so a value trimmed or
// lower-cased on either leg is how a plan stops matching its result and the
// apply fails with "Provider produced inconsistent result after apply" -- the
// v1.9.0 advisory batch, which was exactly this mistake on the server's side.

func TestTheAnsibleAttributesTakeThisResourcesOwnRequiredness(t *testing.T) {
	// Requiredness is DERIVED from the siblings it has to match rather than
	// restated, because restating it is how the two come apart. The bool must
	// look like `enabled` (Optional+Computed: the server always answers with a
	// concrete boolean, so an Optional-only attribute plans null against it and
	// fails every apply that omits it) and the three strings must look like
	// `display_name` (Optional-only: the server answers "" when unset and
	// optString maps that back to null, so null plans against null).
	var resp resource.SchemaResponse
	NewResource().Schema(context.Background(), resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema build error: %v", resp.Diagnostics)
	}

	type shape struct{ required, optional, computed bool }
	of := func(name string) shape {
		t.Helper()
		a, ok := resp.Schema.Attributes[name]
		if !ok {
			t.Fatalf("%s is missing from the catalog item schema", name)
		}
		return shape{a.IsRequired(), a.IsOptional(), a.IsComputed()}
	}

	boolSibling := of("enabled")
	stringSibling := of("display_name")

	// Guard the derivation itself: if a sibling ever stops being the thing this
	// test reasons from, the comparisons below would silently assert the wrong
	// convention rather than failing.
	if boolSibling != (shape{false, true, true}) {
		t.Fatalf("`enabled` is %+v, not Optional+Computed — this test derives the "+
			"bool convention from it, so re-derive before trusting the rest", boolSibling)
	}
	if stringSibling != (shape{false, true, false}) {
		t.Fatalf("`display_name` is %+v, not Optional-only — this test derives the "+
			"string convention from it, so re-derive before trusting the rest", stringSibling)
	}

	if got := of("configure_enabled"); got != boolSibling {
		t.Errorf("configure_enabled is %+v, want %+v to match `enabled`", got, boolSibling)
	}
	for _, name := range []string{"ansible_directory", "default_playbook", "default_limit"} {
		if got := of(name); got != stringSibling {
			t.Errorf("%s is %+v, want %+v to match `display_name`", name, got, stringSibling)
		}
	}

	// None of the four may be Required: every one of them is new on a resource
	// operators already have in state, and a Required attribute would break
	// every existing configuration on upgrade.
	for _, name := range []string{
		"configure_enabled", "ansible_directory", "default_playbook", "default_limit",
	} {
		if of(name).required {
			t.Errorf("%s is Required — that breaks every configuration written before it", name)
		}
	}
}

func TestTheAnsibleHalfReachesTheWireVerbatim(t *testing.T) {
	// The no-normalisation property on the write leg. Two of these values are
	// ones the SERVER refuses, and sending them unchanged is the point: the
	// refusal belongs to the server, and a provider that quietly trimmed them
	// would store something other than what the configuration said.
	for _, tc := range []struct {
		name  string
		value string
	}{
		{"an ordinary subdirectory", "ansible"},
		{"a nested path", "deep/nested/playbooks"},
		{"untrimmed, which the server refuses rather than strips", " ansible "},
		{"a trailing slash, which the server refuses rather than strips", "ansible/"},
		{"mixed case is not folded", "Ansible/SiteConfig"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			attrs, diags := buildCatalogItemAttrs(&catalogItemModel{
				AnsibleDirectory: types.StringValue(tc.value),
				DefaultPlaybook:  types.StringValue(tc.value),
				DefaultLimit:     types.StringValue(tc.value),
			})
			if diags.HasError() {
				t.Fatalf("buildCatalogItemAttrs: %v", diags)
			}
			for _, key := range []string{"ansible-directory", "default-playbook", "default-limit"} {
				if got := attrs[key]; got != tc.value {
					t.Errorf("%s = %#v, want %#v byte for byte", key, got, tc.value)
				}
			}
		})
	}

	t.Run("an ansible limit keeps its own syntax", func(t *testing.T) {
		// `--limit` has a grammar of its own -- patterns, intersection,
		// exclusion -- and none of it is the provider's business.
		const limit = "web:&staging:!web03.example.com"
		attrs, _ := buildCatalogItemAttrs(&catalogItemModel{
			DefaultLimit: types.StringValue(limit),
		})
		if got := attrs["default-limit"]; got != limit {
			t.Errorf("default-limit = %#v, want %#v", got, limit)
		}
	})
}

func TestTheBooleanTravelsIncludingAnExplicitFalse(t *testing.T) {
	// FALSE is the direction that matters. A truthiness guard in the attrs
	// builder would drop it, so an operator turning the configure operation
	// back OFF would silently leave it on -- and the server membership-tests
	// the key, so an absent one means "leave the stored value alone".
	for _, tc := range []struct {
		name string
		in   types.Bool
		want any // nil means the key must be absent
	}{
		{"explicitly on", types.BoolValue(true), true},
		{"explicitly off", types.BoolValue(false), false},
		{"omitted leaves the stored value alone", types.BoolNull(), nil},
		{"unknown is not a declaration", types.BoolUnknown(), nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			attrs, _ := buildCatalogItemAttrs(&catalogItemModel{ConfigureEnabled: tc.in})
			got, present := attrs["configure-enabled"]
			if tc.want == nil {
				if present {
					t.Errorf("configure-enabled = %#v, want the key absent", got)
				}
				return
			}
			if !present {
				t.Fatalf("configure-enabled is absent, want %#v — the declaration never "+
					"reaches the server", tc.want)
			}
			if got != tc.want {
				t.Errorf("configure-enabled = %#v, want %#v", got, tc.want)
			}
		})
	}
}

func TestAnOmittedStringLeavesTheStoredValueAlone(t *testing.T) {
	// #316: the server validates what it is SENT and leaves the rest of the row
	// alone, so an attribute this configuration does not manage must not appear
	// in the PATCH at all. Sending "" instead would clear a stored value nobody
	// asked to clear.
	attrs, _ := buildCatalogItemAttrs(&catalogItemModel{
		AnsibleDirectory: types.StringNull(),
		DefaultPlaybook:  types.StringUnknown(),
		DefaultLimit:     types.StringNull(),
	})
	for _, key := range []string{"ansible-directory", "default-playbook", "default-limit"} {
		if got, present := attrs[key]; present {
			t.Errorf("%s = %#v, want the key absent so the stored value survives", key, got)
		}
	}
}

func TestTheAnsibleHalfRoundTripsUnchanged(t *testing.T) {
	// Both legs with ONE value, which is the property an apply depends on: what
	// a configuration declares is what the server is sent, and what the server
	// answers is what lands in state. Asserting the two halves separately would
	// pass on a pair of mistakes that cancel.
	for _, value := range []string{
		"ansible",
		"deep/nested/playbooks",
		"Ansible/SiteConfig",
		"web:&staging:!web03.example.com",
	} {
		t.Run(value, func(t *testing.T) {
			attrs, diags := buildCatalogItemAttrs(&catalogItemModel{
				AnsibleDirectory: types.StringValue(value),
				DefaultPlaybook:  types.StringValue(value),
				DefaultLimit:     types.StringValue(value),
			})
			if diags.HasError() {
				t.Fatalf("buildCatalogItemAttrs: %v", diags)
			}

			// The server stores what it was sent, so echo exactly the wire the
			// provider produced back as the response.
			var m catalogItemModel
			item := &terrapod.CatalogItem{ID: "c0ffee", Attributes: map[string]any{
				"name":              "x",
				"module-id":         "m",
				"ansible-directory": attrs["ansible-directory"],
				"default-playbook":  attrs["default-playbook"],
				"default-limit":     attrs["default-limit"],
			}}
			if d := readCatalogItemIntoModel(context.Background(), item, &m); d.HasError() {
				t.Fatalf("readCatalogItemIntoModel: %v", d)
			}

			for _, got := range []struct {
				name string
				val  types.String
			}{
				{"ansible_directory", m.AnsibleDirectory},
				{"default_playbook", m.DefaultPlaybook},
				{"default_limit", m.DefaultLimit},
			} {
				if got.val.IsNull() || got.val.ValueString() != value {
					t.Errorf("%s round-tripped to %#v, want %q", got.name, got.val, value)
				}
			}
		})
	}
}

func TestTheReadBackMapsTheServersEmptyStringToNull(t *testing.T) {
	// The server always sends these three and sends "" when unset, so this is
	// what makes Optional-only correct: a configuration that omits the
	// attribute plans null, and null is what comes back.
	var m catalogItemModel
	item := &terrapod.CatalogItem{ID: "c0ffee", Attributes: map[string]any{
		"name":              "x",
		"module-id":         "m",
		"ansible-directory": "",
		"default-playbook":  "",
		"default-limit":     "",
	}}
	if d := readCatalogItemIntoModel(context.Background(), item, &m); d.HasError() {
		t.Fatalf("readCatalogItemIntoModel: %v", d)
	}
	for _, got := range []struct {
		name string
		val  types.String
	}{
		{"ansible_directory", m.AnsibleDirectory},
		{"default_playbook", m.DefaultPlaybook},
		{"default_limit", m.DefaultLimit},
	} {
		if !got.val.IsNull() {
			t.Errorf("%s = %#v, want null — a config that omits it plans null, and a "+
				"concrete \"\" here fails the apply as an inconsistent result", got.name, got.val)
		}
	}
}

func TestTheBooleanReadsBackBothWays(t *testing.T) {
	// Both values, because a read-back hardcoded to either one is invisible to
	// a test that only ever sends the other.
	for _, tc := range []struct {
		name  string
		attrs map[string]any
		want  types.Bool
	}{
		{"true", map[string]any{"configure-enabled": true}, types.BoolValue(true)},
		{"false", map[string]any{"configure-enabled": false}, types.BoolValue(false)},
		// Absent follows `enabled` next to it. The live server always sends the
		// key, so this is the lagging-server case rather than a normal one.
		{"absent", map[string]any{}, types.BoolNull()},
		{"a non-boolean is not coerced", map[string]any{"configure-enabled": "true"}, types.BoolNull()},
	} {
		t.Run(tc.name, func(t *testing.T) {
			attrs := map[string]any{"name": "x", "module-id": "m"}
			for k, v := range tc.attrs {
				attrs[k] = v
			}
			var m catalogItemModel
			item := &terrapod.CatalogItem{ID: "c0ffee", Attributes: attrs}
			if d := readCatalogItemIntoModel(context.Background(), item, &m); d.HasError() {
				t.Fatalf("readCatalogItemIntoModel: %v", d)
			}
			if !m.ConfigureEnabled.Equal(tc.want) {
				t.Errorf("configure_enabled = %#v, want %#v", m.ConfigureEnabled, tc.want)
			}
		})
	}
}

func TestConfigureEnabledNeedsADirectory(t *testing.T) {
	// The cross-field invariant, caught at plan where it can be. The server
	// evaluates it against the row the write would PRODUCE, which is why only
	// the explicitly-empty case is decidable here: a null directory may be a
	// stored one the server is about to keep.
	for _, tc := range []struct {
		name      string
		enabled   types.Bool
		directory types.String
		wantError bool
	}{
		{
			name:      "on with an explicitly empty directory is refused",
			enabled:   types.BoolValue(true),
			directory: types.StringValue(""),
			wantError: true,
		},
		{
			name:      "on with a directory is fine",
			enabled:   types.BoolValue(true),
			directory: types.StringValue("ansible"),
		},
		{
			name:      "off with an empty directory is fine — that is the default item",
			enabled:   types.BoolValue(false),
			directory: types.StringValue(""),
		},
		{
			name: "on with the directory OMITTED is left to the server",
			// Not refusable here: the invariant is evaluated against the merged
			// row, and this item may already hold a directory. A plan-time error
			// would refuse a legitimate switch-on.
			enabled:   types.BoolValue(true),
			directory: types.StringNull(),
		},
		{
			name:      "both omitted is fine — that is every item written before #2019",
			enabled:   types.BoolNull(),
			directory: types.StringNull(),
		},
		{
			name: "an unknown directory is not a declaration",
			// An interpolation that has not resolved yet. Refusing on it would
			// fail a plan that is going to be perfectly valid.
			enabled:   types.BoolValue(true),
			directory: types.StringUnknown(),
		},
		{
			name:      "an unknown boolean is not a declaration",
			enabled:   types.BoolUnknown(),
			directory: types.StringValue(""),
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var resp resource.ValidateConfigResponse
			validateConfigureDirectoryPair(tc.enabled, tc.directory, &resp)

			if got := resp.Diagnostics.HasError(); got != tc.wantError {
				t.Fatalf("HasError() = %t, want %t (%v)", got, tc.wantError, resp.Diagnostics)
			}
			if !tc.wantError {
				return
			}
			// The message has to name both attributes, or an operator cannot
			// tell which of the two to change.
			detail := resp.Diagnostics.Errors()[0].Detail()
			for _, want := range []string{"configure_enabled", "ansible_directory"} {
				if !strings.Contains(detail, want) {
					t.Errorf("the refusal does not name %s: %q", want, detail)
				}
			}
		})
	}
}

func TestValidateConfigDrivesThePairCheck(t *testing.T) {
	// The CALL SITE, not the predicate. Every case above calls
	// validateConfigureDirectoryPair directly, so dropping the one line in
	// ValidateConfig that calls it would leave all of them green while the
	// provider validated nothing. This drives the real method through a real
	// config.
	ctx := context.Background()
	r := NewResource()

	cfg := configWith(t, ctx, r, map[string]tftypes.Value{
		"configure_enabled": tftypes.NewValue(tftypes.Bool, true),
		"ansible_directory": tftypes.NewValue(tftypes.String, ""),
	})

	var resp resource.ValidateConfigResponse
	r.(interface {
		ValidateConfig(context.Context, resource.ValidateConfigRequest, *resource.ValidateConfigResponse)
	}).ValidateConfig(ctx, resource.ValidateConfigRequest{Config: cfg}, &resp)

	if !resp.Diagnostics.HasError() {
		t.Fatal("ValidateConfig accepted `configure_enabled = true` with an empty " +
			"`ansible_directory` — the pair check is not wired into it")
	}

	// And the negative direction, so the wiring cannot be satisfied by a method
	// that simply always errors.
	cfg = configWith(t, ctx, r, map[string]tftypes.Value{
		"configure_enabled": tftypes.NewValue(tftypes.Bool, true),
		"ansible_directory": tftypes.NewValue(tftypes.String, "ansible"),
	})
	var ok resource.ValidateConfigResponse
	r.(interface {
		ValidateConfig(context.Context, resource.ValidateConfigRequest, *resource.ValidateConfigResponse)
	}).ValidateConfig(ctx, resource.ValidateConfigRequest{Config: cfg}, &ok)
	if ok.Diagnostics.HasError() {
		t.Fatalf("ValidateConfig refused a valid pair: %v", ok.Diagnostics)
	}
}

// configWith builds a resource config whose every attribute is null except the
// ones named, so a ValidateConfig test states only what it is about.
func configWith(t *testing.T, ctx context.Context, r resource.Resource, set map[string]tftypes.Value) tfsdk.Config {
	t.Helper()
	var sr resource.SchemaResponse
	r.Schema(ctx, resource.SchemaRequest{}, &sr)
	if sr.Diagnostics.HasError() {
		t.Fatalf("schema: %v", sr.Diagnostics)
	}

	objType, ok := sr.Schema.Type().TerraformType(ctx).(tftypes.Object)
	if !ok {
		t.Fatal("the catalog item schema did not render as an object type")
	}
	// Every name the caller asked for must exist, or the test would silently
	// assert nothing about a misspelled attribute.
	for name := range set {
		if _, ok := objType.AttributeTypes[name]; !ok {
			t.Fatalf("%s is not an attribute of the catalog item schema", name)
		}
	}
	vals := make(map[string]tftypes.Value, len(objType.AttributeTypes))
	for name, aType := range objType.AttributeTypes {
		if v, override := set[name]; override {
			vals[name] = v
			continue
		}
		vals[name] = tftypes.NewValue(aType, nil)
	}
	return tfsdk.Config{Schema: sr.Schema, Raw: tftypes.NewValue(objType, vals)}
}
