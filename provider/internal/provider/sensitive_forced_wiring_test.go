package provider

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
)

// Every resource whose `sensitive` attribute the SERVER can force to true must
// carry the plan modifier that forces it in the plan as well.
//
// This is a wiring gate, not a logic test: schema.golden records an attribute's
// required/optional/computed/sensitive/type and NOT its plan modifiers, so
// dropping the modifier changes no snapshot and breaks no unit test — it just
// silently reintroduces "Provider produced inconsistent result after apply" on
// the documented HCL, and then a sensitive diff that never converges.
//
// A resource joins this list when it gains a `value_source` or a git-auth
// `category`, which are the two things the server forces sensitive on.
func TestSensitiveIsForcedInThePlanWhereverTheServerForcesIt(t *testing.T) {
	mustHaveModifier := map[string]bool{
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
		if !mustHaveModifier[md.TypeName] {
			continue
		}
		seen[md.TypeName] = true

		var sr resource.SchemaResponse
		r.Schema(ctx, resource.SchemaRequest{}, &sr)
		if sr.Diagnostics.HasError() {
			t.Fatalf("%s: schema error: %v", md.TypeName, sr.Diagnostics)
		}

		attr, ok := sr.Schema.Attributes["sensitive"]
		if !ok {
			t.Errorf("%s: no `sensitive` attribute — has the resource changed shape?", md.TypeName)
			continue
		}
		b, ok := attr.(schema.BoolAttribute)
		if !ok {
			t.Errorf("%s: `sensitive` is not a BoolAttribute", md.TypeName)
			continue
		}
		if len(b.PlanModifiers) == 0 {
			t.Errorf("%s: `sensitive` has no plan modifiers. It needs "+
				"planmods.SensitiveForSecretBearingVariable(), or a vault-sourced or "+
				"git-auth variable plans false and applies true.", md.TypeName)
		}
	}

	for name := range mustHaveModifier {
		if !seen[name] {
			t.Errorf("%s is listed here but the provider registers no such resource — "+
				"was it renamed or removed?", name)
		}
	}
}
