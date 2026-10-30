package catalog_instance

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/types"
	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// `version_pin` is Optional+Computed, so an explicit "" in config plans as ""
// and the apply must come back with "" -- a null read-back raises "Provider
// produced inconsistent result after apply" (#2310).
//
// It drives the write leg and the read leg together, because the round trip is
// where this attribute is interesting: the write leg sends "" (it sends
// whenever the value is not null), the server stores that as NULL, and the read
// leg therefore has to answer "" to a null payload. Either leg read on its own
// looks correct.
func TestVersionPinRoundTripsAnExplicitEmptyString(t *testing.T) {
	m := catalogInstanceModel{
		Name:        types.StringValue("x"),
		AgentPoolID: types.StringValue("apool-1"),
		VersionPin:  types.StringValue(""),
	}

	attrs := buildProvisionAttrs(&m)
	v, ok := attrs["version-pin"]
	if !ok {
		t.Fatalf("version-pin was not sent; an explicit \"\" must reach the wire")
	}
	if v != "" {
		t.Errorf("version-pin sent as %#v, want \"\"", v)
	}

	// The server's provision and reconfigure paths both do
	// `str(vp) if vp else None` into a NULLABLE column, so the "" sent above is
	// stored as NULL and read back as JSON null -- which is the payload the
	// round trip actually passes through.
	var back catalogInstanceModel
	inst := &terrapod.CatalogInstance{ID: "ws-c0ffee", Attributes: map[string]any{
		"name": "x", "agent-pool-id": "apool-1", "catalog-version-pin": nil,
	}}
	if d := readCatalogInstanceIntoModel(context.Background(), inst, &back); d.HasError() {
		t.Fatalf("readCatalogInstanceIntoModel: %v", d)
	}
	if back.VersionPin != m.VersionPin {
		t.Errorf("the round trip changed the value: planned %v, got %v", m.VersionPin, back.VersionPin)
	}
}

// Every shape the server can hand back, measured rather than assumed:
//
//	catalog_version_pin is `str(vp) if vp else None` into a NULLABLE column
//	(models.py: `Mapped[str | None] ... nullable=True`) and is serialised RAW
//	(`"catalog-version-pin": ws.catalog_version_pin`) -- no `or ""` to flatten
//	it. So an explicit "" is stored as NULL and comes back as JSON null, and
//	null is the NORMAL payload for this attribute rather than an edge case.
//
// A read-back that answered null for a null payload would therefore fail the
// apply on the very shape the server produces most of the time, which is why
// the nil case below is the one that matters and a test written only against ""
// would miss the defect entirely.
//
// Note the key: the instance RESPONSE carries the pin as "catalog-version-pin".
// The bare "version-pin" is an inbound provision/reconfigure attribute only,
// and reading the wrong one caused perpetual plan drift.
func TestVersionPinReadsBackAsAnEmptyStringForEveryServerShape(t *testing.T) {
	for _, tc := range []struct {
		name  string
		attrs map[string]any
	}{
		{"an empty string", map[string]any{"catalog-version-pin": ""}},
		{"a JSON null, as the nullable column serialises an explicit empty", map[string]any{
			"catalog-version-pin": nil,
		}},
		{"absent entirely", map[string]any{}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			tc.attrs["name"] = "x"
			tc.attrs["agent-pool-id"] = "apool-1"

			var m catalogInstanceModel
			inst := &terrapod.CatalogInstance{ID: "ws-c0ffee", Attributes: tc.attrs}
			if d := readCatalogInstanceIntoModel(context.Background(), inst, &m); d.HasError() {
				t.Fatalf("readCatalogInstanceIntoModel: %v", d)
			}
			if m.VersionPin.IsNull() {
				t.Errorf("version_pin read back as null; it is Optional+Computed, so a " +
					"null result cannot satisfy an explicit \"\" in config")
			}
			if m.VersionPin.ValueString() != "" {
				t.Errorf("version_pin read back as %q, want \"\"", m.VersionPin.ValueString())
			}
		})
	}
}
