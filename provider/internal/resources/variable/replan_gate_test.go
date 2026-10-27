package variable

import (
	"context"
	"sort"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
)

// Every schema attribute is compared, carried, or named immutable -- nothing
// may simply be absent from all three.
//
// This is the gate the `apply_only` omission needed and did not have. An
// attribute left out of `changeDetecting` makes a change to it alone look like
// a no-op to KeepComputedWhenUnchanged, which then pins `version_id` and
// `updated_at` to their state values as KNOWN. `updated_at` carries `onupdate`
// on the server, so the write moves it regardless and Terraform fails the apply
// with "Provider produced inconsistent result after apply". Nothing else can
// see this: schema.golden records an attribute's required/optional/computed and
// not which plan-time list it belongs to, so the omission breaks no snapshot.
//
// The membership is derived from the schema rather than listed here, so a
// future attribute is in scope the moment it is declared.
func TestEveryAttributeIsAccountedForInTheRePlanGate(t *testing.T) {
	var sr resource.SchemaResponse
	(&variableResource{}).Schema(context.Background(), resource.SchemaRequest{}, &sr)
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
			"if it never moves after create. Leaving it out makes a change to it "+
			"alone plan a stale version_id/updated_at and fail the apply.", name)
	}

	// The other direction: a name listed here that the schema no longer has is
	// a list that has stopped describing the resource.
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
