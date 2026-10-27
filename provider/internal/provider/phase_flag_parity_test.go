package provider

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/defaults"
)

// Both phase flags are declared alike, on every resource that carries either.
//
// `apply_only` (#2189) and `configure_only` (#2190) are two slots of one thing
// -- which run phases receive this variable -- and a key is held in exactly one
// of three states: ordinary (every phase), apply-only (apply and configure), or
// configure-only (configure alone). `configure_only` is therefore a THIRD slot
// rather than a rename, because a configure already receives every apply-only
// row and so `apply_only` cannot express "not in an apply either".
//
// Declaring them differently is how one of the three states becomes unreachable
// or behaves unlike its sibling for no stated reason. schema.golden records
// each attribute's own shape but asserts nothing about the two AGREEING, so a
// drift between them changes a snapshot line that reads as deliberate.
//
// Setting both on one variable is refused by the server with a 422 and is not
// checked at plan time -- `apply_only` has never had such a validator, and
// adding one for only half the pair would be its own asymmetry.
func TestThePhaseFlagsAreDeclaredAlike(t *testing.T) {
	// A resource joins this list when it gains either flag.
	carriesPhaseFlags := map[string]bool{
		"terrapod_variable":              true,
		"terrapod_variable_set_variable": true,
	}

	ctx := context.Background()
	p := New("test")()
	seen := map[string]bool{}

	for _, factory := range p.Resources(ctx) {
		r := factory()
		var md resource.MetadataResponse
		r.Metadata(ctx, resource.MetadataRequest{ProviderTypeName: providerTypeName}, &md)
		if !carriesPhaseFlags[md.TypeName] {
			continue
		}
		seen[md.TypeName] = true

		var sr resource.SchemaResponse
		r.Schema(ctx, resource.SchemaRequest{}, &sr)
		if sr.Diagnostics.HasError() {
			t.Fatalf("%s: schema error: %v", md.TypeName, sr.Diagnostics)
		}

		applyOnly, ok := boolAttr(t, md.TypeName, sr.Schema, "apply_only")
		if !ok {
			continue
		}
		configureOnly, ok := boolAttr(t, md.TypeName, sr.Schema, "configure_only")
		if !ok {
			continue
		}

		if configureOnly.Optional != applyOnly.Optional {
			t.Errorf("%s: configure_only.Optional=%v but apply_only.Optional=%v",
				md.TypeName, configureOnly.Optional, applyOnly.Optional)
		}
		if configureOnly.Computed != applyOnly.Computed {
			t.Errorf("%s: configure_only.Computed=%v but apply_only.Computed=%v",
				md.TypeName, configureOnly.Computed, applyOnly.Computed)
		}
		if configureOnly.Required != applyOnly.Required {
			t.Errorf("%s: configure_only.Required=%v but apply_only.Required=%v",
				md.TypeName, configureOnly.Required, applyOnly.Required)
		}
		if configureOnly.Sensitive != applyOnly.Sensitive {
			t.Errorf("%s: configure_only.Sensitive=%v but apply_only.Sensitive=%v",
				md.TypeName, configureOnly.Sensitive, applyOnly.Sensitive)
		}
		// A Computed+Optional bool with no default plans unknown on a create,
		// so both need one and both need the SAME one: a key is configure-only
		// or apply-only only when it says so.
		if (configureOnly.Default == nil) != (applyOnly.Default == nil) {
			t.Errorf("%s: one phase flag has a default and the other does not "+
				"(configure_only=%v, apply_only=%v)",
				md.TypeName, configureOnly.Default != nil, applyOnly.Default != nil)
		}
		if configureOnly.Default != nil && applyOnly.Default != nil {
			cv := defaultBool(ctx, t, md.TypeName, "configure_only", configureOnly)
			av := defaultBool(ctx, t, md.TypeName, "apply_only", applyOnly)
			if cv != av {
				t.Errorf("%s: configure_only defaults to %v but apply_only defaults to %v",
					md.TypeName, cv, av)
			}
			if cv {
				t.Errorf("%s: configure_only defaults to true -- a variable is "+
					"withheld from a plan and an apply only when it says so", md.TypeName)
			}
		}
	}

	for name := range carriesPhaseFlags {
		if !seen[name] {
			t.Errorf("%s is listed here but the provider registers no such resource -- "+
				"was it renamed or removed?", name)
		}
	}
}

func boolAttr(t *testing.T, typeName string, s schema.Schema, name string) (schema.BoolAttribute, bool) {
	t.Helper()
	attr, ok := s.Attributes[name]
	if !ok {
		t.Errorf("%s: no %q attribute", typeName, name)
		return schema.BoolAttribute{}, false
	}
	b, ok := attr.(schema.BoolAttribute)
	if !ok {
		t.Errorf("%s: %q is not a BoolAttribute", typeName, name)
		return schema.BoolAttribute{}, false
	}
	return b, true
}

func defaultBool(ctx context.Context, t *testing.T, typeName, name string, b schema.BoolAttribute) bool {
	t.Helper()
	var resp defaults.BoolResponse
	b.Default.DefaultBool(ctx, defaults.BoolRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("%s: resolving the default for %q: %v", typeName, name, resp.Diagnostics)
	}
	return resp.PlanValue.ValueBool()
}
