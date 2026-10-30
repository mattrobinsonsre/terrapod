package catalog_item

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/types"
	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// The #2307 defect, end to end, for the three strings that shipped with it:
// `display_name`, `description` and `default_version_pin`. Setting any of them
// to an explicit "" planned as "" and applied as null, so the framework raised
// "Provider produced inconsistent result after apply".
//
// It drives the write leg and the read leg together, because the defect lived in
// their disagreement rather than in either one: the write leg sent "" (it sends
// whenever the value is not null), and the read leg collapsed it back to null.
func TestTheShippedThreeStringsRoundTripAnExplicitEmptyString(t *testing.T) {
	m := catalogItemModel{
		Name:              types.StringValue("x"),
		ModuleID:          types.StringValue("m"),
		DisplayName:       types.StringValue(""),
		Description:       types.StringValue(""),
		DefaultVersionPin: types.StringValue(""),
	}

	attrs, d := buildCatalogItemAttrs(&m)
	if d.HasError() {
		t.Fatalf("buildCatalogItemAttrs: %v", d)
	}
	for _, key := range []string{"display-name", "description", "default-version-pin"} {
		v, ok := attrs[key]
		if !ok {
			t.Fatalf("%s was not sent; an explicit \"\" must reach the wire", key)
		}
		if v != "" {
			t.Errorf("%s sent as %#v, want \"\"", key, v)
		}
	}

	var back catalogItemModel
	item := &terrapod.CatalogItem{ID: "c0ffee", Attributes: map[string]any{
		"name": "x", "module-id": "m",
		"display-name": "", "description": "", "default-version-pin": "",
	}}
	if d := readCatalogItemIntoModel(context.Background(), item, &back); d.HasError() {
		t.Fatalf("readCatalogItemIntoModel: %v", d)
	}
	if back.DisplayName != m.DisplayName ||
		back.Description != m.Description ||
		back.DefaultVersionPin != m.DefaultVersionPin {
		t.Errorf("the round trip changed the value: planned %v/%v/%v, got %v/%v/%v",
			m.DisplayName, m.Description, m.DefaultVersionPin,
			back.DisplayName, back.Description, back.DefaultVersionPin)
	}
}

// The three are NOT uniform on the server, and the read-back has to be right for
// every shape it can actually be handed -- measured rather than assumed:
//
//	display_name / description : `str(x or "")`, a NOT NULL column defaulting to
//	                             "", serialised with `or ""`. Always a string.
//	default_version_pin        : `str(x) if x else None` into a NULLABLE column,
//	                             serialised raw -- so an explicit "" is stored as
//	                             NULL and comes back as JSON null, not "".
//
// So the pin's own round trip passes through null, and a read-back that answered
// null for a null payload would fail the apply for the attribute whose server
// path makes that payload the normal one.
func TestEveryServerShapeReadsBackAsAnEmptyStringRatherThanNull(t *testing.T) {
	for _, tc := range []struct {
		name  string
		attrs map[string]any
	}{
		{"an empty string, as the two NOT NULL columns serialise", map[string]any{
			"display-name": "", "description": "", "default-version-pin": "",
		}},
		{"a JSON null, as the nullable pin serialises an explicit empty", map[string]any{
			"display-name": nil, "description": nil, "default-version-pin": nil,
		}},
		{"absent entirely", map[string]any{}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			tc.attrs["name"] = "x"
			tc.attrs["module-id"] = "m"
			var m catalogItemModel
			item := &terrapod.CatalogItem{ID: "c0ffee", Attributes: tc.attrs}
			if d := readCatalogItemIntoModel(context.Background(), item, &m); d.HasError() {
				t.Fatalf("readCatalogItemIntoModel: %v", d)
			}
			for label, got := range map[string]types.String{
				"display_name":        m.DisplayName,
				"description":         m.Description,
				"default_version_pin": m.DefaultVersionPin,
			} {
				if got.IsNull() {
					t.Errorf("%s read back as null; these are Optional+Computed, so a "+
						"null result cannot satisfy an explicit \"\" in config", label)
				}
				if got.ValueString() != "" {
					t.Errorf("%s read back as %q, want \"\"", label, got.ValueString())
				}
			}
		})
	}
}
