package variable_set_variable

import (
	"context"
	"sort"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
)

// Every schema attribute is compared, carried, or named immutable. See the
// identical gate on terrapod_variable for why an attribute in none of the three
// produces "Provider produced inconsistent result after apply" and why no
// snapshot can catch it.
func TestEveryAttributeIsAccountedForInTheRePlanGate(t *testing.T) {
	var sr resource.SchemaResponse
	(&variableSetVariableResource{}).Schema(context.Background(), resource.SchemaRequest{}, &sr)
	if sr.Diagnostics.HasError() {
		t.Fatalf("schema error: %v", sr.Diagnostics)
	}
	if len(sr.Schema.Attributes) == 0 {
		t.Fatal("the schema declares no attributes -- this gate would pass on nothing")
	}

	accounted := map[string]string{}
	for _, name := range changeDetecting {
		accounted[name] = "changeDetecting"
	}
	for _, name := range carriedWhenUnchanged {
		accounted[name] = "carriedWhenUnchanged"
	}
	for _, name := range serverAssignedAndImmutable {
		accounted[name] = "serverAssignedAndImmutable"
	}

	var missing []string
	for name := range sr.Schema.Attributes {
		if _, ok := accounted[name]; !ok {
			missing = append(missing, name)
		}
	}
	sort.Strings(missing)
	for _, name := range missing {
		t.Errorf("attribute %q is in no plan-time list. Add it to changeDetecting "+
			"if a write can change it (nearly always), to carriedWhenUnchanged if "+
			"the server moves it on every write, or to serverAssignedAndImmutable "+
			"if it never moves after create.", name)
	}

	var stale []string
	for name := range accounted {
		if _, ok := sr.Schema.Attributes[name]; !ok {
			stale = append(stale, name)
		}
	}
	sort.Strings(stale)
	for _, name := range stale {
		t.Errorf("%q is listed in %s but the schema declares no such attribute -- "+
			"was it renamed or removed?", name, accounted[name])
	}
}

// Both phase flags decide which run phases receive a variable, so a write can
// change either and both belong in the compare list.
func TestBothPhaseFlagsAreChangeDetecting(t *testing.T) {
	in := map[string]bool{}
	for _, name := range changeDetecting {
		in[name] = true
	}
	for _, name := range []string{"apply_only", "configure_only"} {
		if !in[name] {
			t.Errorf("%q is not in changeDetecting, so flipping it alone plans a "+
				"stale updated_at and the apply contradicts the plan", name)
		}
	}
}
