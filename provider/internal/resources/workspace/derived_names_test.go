package workspace

import (
	"context"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// A Computed name derived from a configurable id is the one shape that looks
// safe for `UseStateForUnknown` and is not: it does not move on an ordinary
// PATCH, so it reads as stable, but it moves on exactly the applies that change
// its id -- and the plan then promises the OLD name against a read-back of the
// new one, which the framework refuses as "Provider produced inconsistent
// result after apply", AFTER the server-side change has succeeded (#2110).
//
// The class is DERIVED from the schema here rather than listed, because listing
// it is what failed: `agent_pool_name` shipped unguarded while the schema
// comment beside it named the attribute as safe, with the invariant "only
// changes when agent_pool_id changes" -- a statement of the bug dressed as a
// stability argument. A gate that trusted that comment would have agreed with it.
func TestEveryDerivedNameIsGuarded(t *testing.T) {
	var r workspaceResource
	resp := &resource.SchemaResponse{}
	r.Schema(context.Background(), resource.SchemaRequest{}, resp)
	attrs := resp.Schema.Attributes

	guarded := map[string]bool{}
	for _, d := range derivedNames {
		guarded[d.Name] = true
	}

	// The shape: `<x>_name`, Computed and NOT configurable, beside a
	// configurable `<x>_id`. Conservative on purpose -- an attribute of this
	// shape must be guarded whether or not it currently carries
	// `UseStateForUnknown`, because adding that modifier later is a one-line
	// change that would reintroduce the defect silently.
	found := 0
	for name, a := range attrs {
		if !strings.HasSuffix(name, "_name") {
			continue
		}
		if !a.IsComputed() || a.IsOptional() || a.IsRequired() {
			continue
		}
		id := strings.TrimSuffix(name, "_name") + "_id"
		src, ok := attrs[id]
		if !ok || !src.IsOptional() {
			continue
		}
		found++
		if !guarded[name] {
			t.Errorf("%q is Computed and derived from the configurable %q, but is not in "+
				"`derivedNames` — an apply that changes %q will plan the old %q and fail "+
				"with \"Provider produced inconsistent result after apply\" after the "+
				"server-side change has already succeeded (#2110)", name, id, id, name)
		}
	}

	// A floor, so a rename cannot make this gate pass by inspecting nothing.
	if found < 2 {
		t.Errorf("found only %d derived name(s) in the schema; expected at least 2 "+
			"(agent_pool_name, vcs_connection_name). If they were renamed, fix the shape "+
			"this test looks for rather than lowering the floor", found)
	}
}

// The ledger must not name something the schema does not have, or an entry
// quietly stops guarding anything.
func TestTheLedgerNamesRealAttributes(t *testing.T) {
	var r workspaceResource
	resp := &resource.SchemaResponse{}
	r.Schema(context.Background(), resource.SchemaRequest{}, resp)
	attrs := resp.Schema.Attributes

	for _, d := range derivedNames {
		if _, ok := attrs[d.Name]; !ok {
			t.Errorf("derivedNames names %q, which is not an attribute of this resource", d.Name)
		}
		if len(d.Sources) == 0 {
			t.Errorf("derivedNames entry %q has no sources, so it can never be marked unknown", d.Name)
		}
		for _, src := range d.Sources {
			if _, ok := attrs[src]; !ok {
				t.Errorf("derivedNames[%q] names source %q, which is not an attribute of "+
					"this resource — the comparison would silently never fire", d.Name, src)
			}
		}
	}
}

// `onlyPool` answers "is the set exactly this one pool", which is NOT the same
// question as "would applying the singular drop anything" -- and conflating the
// two is half of #2110. `poolSetSize` answers the second.
func TestPoolSetSize(t *testing.T) {
	strList := func(vals ...string) types.List {
		elems := make([]attr.Value, 0, len(vals))
		for _, v := range vals {
			elems = append(elems, types.StringValue(v))
		}
		return types.ListValueMust(types.StringType, elems)
	}

	cases := []struct {
		name string
		val  attr.Value
		want int
	}{
		{"null", types.ListNull(types.StringType), 0},
		{"unknown", types.ListUnknown(types.StringType), 0},
		{"empty", strList(), 0},
		{"one pool — a 1->1 change drops nothing, so no warning", strList("apool-a"), 1},
		{"two pools — the singular would drop one, so warn", strList("apool-a", "apool-b"), 2},
		{"not a list", types.StringValue("apool-a"), 0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := poolSetSize(tc.val); got != tc.want {
				t.Errorf("poolSetSize(%v) = %d, want %d", tc.val, got, tc.want)
			}
		})
	}

	// The contrast that is the bug: for a set of ONE DIFFERENT pool, `onlyPool`
	// says false (it is not the configured pool) while `poolSetSize` says 1 (so
	// nothing is dropped). The old code read the first as "several pools".
	one := strList("apool-a")
	if onlyPool(one, "apool-b") {
		t.Fatal("onlyPool should be false for a different pool")
	}
	if poolSetSize(one) > 1 {
		t.Error("a single-pool set must not read as several — that is the false warning in #2110")
	}
}
