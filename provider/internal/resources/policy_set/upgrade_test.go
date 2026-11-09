package policy_set

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/tfsdk"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/mattrobinsonsre/terrapod/provider/internal/stateupgrade"
)

// #2452, second instance. `terrapod_policy_set` took the identical change as
// `terrapod_role` in #1480 and was found by auditing every resource for a
// type change across the 2.0 boundary rather than by anyone hitting it --
// which is the point of having audited.

func v0State(t *testing.T, labels map[string]string) tfsdk.State {
	t.Helper()
	ctx := context.Background()

	flat := types.MapNull(stateupgrade.LabelsV0Type)
	if labels != nil {
		vals := make(map[string]attr.Value, len(labels))
		for k, v := range labels {
			vals[k] = types.StringValue(v)
		}
		m, d := types.MapValue(stateupgrade.LabelsV0Type, vals)
		if d.HasError() {
			t.Fatalf("building the v0 map: %v", d)
		}
		flat = m
	}

	// Built through the PRIOR schema, so every attribute is whatever that
	// schema says -- a hand-written struct literal would go stale the moment
	// the model gains a field, and fail as a decode error rather than as
	// anything to do with this upgrade.
	state := tfsdk.State{Schema: *priorSchemaV0()}
	if d := state.Set(ctx, nullModel(t, flat)); d.HasError() {
		t.Fatalf("building v0 state: %v", d)
	}
	return state
}

func upgrade(t *testing.T, prior tfsdk.State) policySetModel {
	t.Helper()
	ctx := context.Background()
	resp := &resource.UpgradeStateResponse{State: tfsdk.State{Schema: currentSchema()}}
	upgradePolicySetV0toV1(ctx, resource.UpgradeStateRequest{State: &prior}, resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("upgrading: %v", resp.Diagnostics)
	}
	var out policySetModel
	if d := resp.State.Get(ctx, &out); d.HasError() {
		t.Fatalf("reading the upgraded state: %v", d)
	}
	return out
}

func TestAScalarLabelBecomesAOneElementList(t *testing.T) {
	out := upgrade(t, v0State(t, map[string]string{"env": "prod"}))

	for name, m := range map[string]types.Map{"allow": out.AllowLabels, "deny": out.DenyLabels} {
		if m.IsNull() {
			t.Fatalf("%s_labels came back null", name)
		}
		if got := m.ElementType(context.Background()); !got.Equal(stateupgrade.LabelsV1Type) {
			t.Fatalf("%s_labels element type is %v, want %v", name, got, stateupgrade.LabelsV1Type)
		}
		var got map[string][]string
		if d := m.ElementsAs(context.Background(), &got, false); d.HasError() {
			t.Fatalf("reading back: %v", d)
		}
		if len(got["env"]) != 1 || got["env"][0] != "prod" {
			t.Fatalf(`%s_labels env = %v, want ["prod"]`, name, got["env"])
		}
	}
}

func TestNoLabelsStaysNullAtTheNEWElementType(t *testing.T) {
	out := upgrade(t, v0State(t, nil))
	for name, m := range map[string]types.Map{"allow": out.AllowLabels, "deny": out.DenyLabels} {
		if !m.IsNull() {
			t.Fatalf("%s_labels should still be null, got %v", name, m)
		}
		if got := m.ElementType(context.Background()); !got.Equal(stateupgrade.LabelsV1Type) {
			t.Fatalf("%s_labels null map is typed %v, want %v", name, got, stateupgrade.LabelsV1Type)
		}
	}
}

func TestTheResourceDeclaresTheUpgradeAtVersionZero(t *testing.T) {
	if _, ok := (&policySetResource{}).UpgradeState(context.Background())[0]; !ok {
		t.Fatal("no upgrader from version 0")
	}
	if got := currentSchema().Version; got != schemaVersion || got == 0 {
		t.Fatalf("schema version is %d, want %d and non-zero", got, schemaVersion)
	}
}

func TestThePriorSchemaStillDescribesVersionZero(t *testing.T) {
	prior, current := priorSchemaV0(), currentSchema()
	if len(prior.Attributes) != len(current.Attributes) {
		t.Fatalf("prior has %d attributes, current %d -- the derivation is no longer exact",
			len(prior.Attributes), len(current.Attributes))
	}
	if prior.Version != 0 {
		t.Errorf("prior schema version is %d, want 0", prior.Version)
	}
	for _, name := range labelAttributes {
		a, ok := prior.Attributes[name].(interface{ GetType() attr.Type })
		if !ok {
			t.Fatalf("%q is not a typed attribute", name)
		}
		want := types.MapType{ElemType: stateupgrade.LabelsV0Type}
		if got := a.GetType(); !got.Equal(want) {
			t.Errorf("prior %q is %v, want %v", name, got, want)
		}
	}
}
