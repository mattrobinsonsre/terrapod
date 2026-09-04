package role

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// #1457: a role rule binds each label key to the values that satisfy it, so
// { env = ["prod", "stg"] } means "env is prod OR stg". The server has always
// enforced that; the provider could neither express it nor read a role that
// used one, because allow_labels/deny_labels were a map of plain strings.
//
// These exercise the projection in both directions. The gap was a test gap
// before it was a code gap — every existing test used a single value per key,
// which is exactly the shape that worked.

func listOf(t *testing.T, vals ...string) types.List {
	t.Helper()
	elems := make([]attr.Value, 0, len(vals))
	for _, v := range vals {
		elems = append(elems, types.StringValue(v))
	}
	l, d := types.ListValue(types.StringType, elems)
	if d.HasError() {
		t.Fatalf("building list: %v", d)
	}
	return l
}

func mapOf(t *testing.T, entries map[string]types.List) types.Map {
	t.Helper()
	vals := make(map[string]attr.Value, len(entries))
	for k, v := range entries {
		vals[k] = v
	}
	m, d := types.MapValue(types.ListType{ElemType: types.StringType}, vals)
	if d.HasError() {
		t.Fatalf("building map: %v", d)
	}
	return m
}

func TestSeveralValuesForOneKeyReachTheRequest(t *testing.T) {
	got := mapFromTFMap(mapOf(t, map[string]types.List{
		"env": listOf(t, "prod", "stg"),
	}))

	if len(got["env"]) != 2 || got["env"][0] != "prod" || got["env"][1] != "stg" {
		t.Errorf("env = %v, want [prod stg]", got["env"])
	}
}

func TestASingleValueStillProjectsToAOneElementList(t *testing.T) {
	// The common case must keep working — this is what every pre-2.0 config
	// wrote, just spelled as a one-element list now.
	got := mapFromTFMap(mapOf(t, map[string]types.List{
		"team": listOf(t, "sre"),
	}))

	if len(got["team"]) != 1 || got["team"][0] != "sre" {
		t.Errorf("team = %v, want [sre]", got["team"])
	}
}

func TestOrderIsPreservedWithinAKey(t *testing.T) {
	// Not cosmetic: the values are shown back to the operator in the role
	// editor and in a plan diff, so reordering them would read as a change.
	got := mapFromTFMap(mapOf(t, map[string]types.List{
		"env": listOf(t, "c", "a", "b"),
	}))

	want := []string{"c", "a", "b"}
	for i := range want {
		if got["env"][i] != want[i] {
			t.Fatalf("env = %v, want %v", got["env"], want)
		}
	}
}

func TestAnAbsentMapClearsRatherThanOmits(t *testing.T) {
	// Update semantics: no labels in HCL means "clear them on the server", not
	// "leave whatever is there". Omitting allow_labels used to clear them by
	// accident; this pins the deliberate version of that behaviour.
	got := mapFromTFMapOrEmpty(types.MapNull(types.ListType{ElemType: types.StringType}))
	if got == nil {
		t.Fatal("a null map must project to an empty map, not nil — nil omits the field")
	}
	if len(got) != 0 {
		t.Errorf("want an empty map, got %v", got)
	}
}

func TestReadBackRendersListsTerraformCanConsume(t *testing.T) {
	val, d := types.MapValueFrom(
		context.Background(),
		types.ListType{ElemType: types.StringType},
		map[string][]string{"env": {"prod", "stg"}},
	)
	if d.HasError() {
		t.Fatalf("a list-valued rule must render back into state: %v", d)
	}
	if val.IsNull() || len(val.Elements()) != 1 {
		t.Fatalf("unexpected map: %#v", val)
	}
}
