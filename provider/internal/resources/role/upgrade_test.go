package role

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/tfsdk"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/mattrobinsonsre/terrapod/provider/internal/stateupgrade"
)

// #2452: `allow_labels`/`deny_labels` went from map(string) to
// map(list(string)) in #1480 with no state upgrader, so every plan against a
// 1.x state failed `Unable to Read Previously Saved State`. It failed even
// after the configuration had been updated -- the configuration was fine, the
// stored state could not be read.
//
// These drive the upgrader itself. Asserting only that `Version` is 1 would
// pass with an upgrader that mangles the state.

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

	state := tfsdk.State{Schema: *priorSchemaV0()}
	model := roleModel{
		ID:                  types.StringValue("platform"),
		Name:                types.StringValue("platform"),
		Description:         types.StringValue("the one that broke"),
		AllowAll:            types.BoolValue(false),
		AllowLabels:         flat,
		AllowNames:          types.ListNull(types.StringType),
		DenyLabels:          flat,
		DenyNames:           types.ListNull(types.StringType),
		WorkspacePermission: types.StringValue("read"),
		PoolPermission:      types.StringNull(),
		RegistryPermission:  types.StringNull(),
		CatalogPermission:   types.StringNull(),
		Capabilities:        types.SetNull(types.StringType),
		BuiltIn:             types.BoolValue(false),
		CreatedAt:           types.StringValue("2026-01-01T00:00:00Z"),
		UpdatedAt:           types.StringValue("2026-01-01T00:00:00Z"),
	}
	if d := state.Set(ctx, &model); d.HasError() {
		t.Fatalf("building v0 state: %v", d)
	}
	return state
}

func upgrade(t *testing.T, prior tfsdk.State) roleModel {
	t.Helper()
	ctx := context.Background()
	resp := &resource.UpgradeStateResponse{State: tfsdk.State{Schema: currentSchema()}}
	upgradeRoleV0toV1(ctx, resource.UpgradeStateRequest{State: &prior}, resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("upgrading: %v", resp.Diagnostics)
	}
	var out roleModel
	if d := resp.State.Get(ctx, &out); d.HasError() {
		t.Fatalf("reading the upgraded state: %v", d)
	}
	return out
}

func TestAScalarLabelBecomesAOneElementList(t *testing.T) {
	out := upgrade(t, v0State(t, map[string]string{"team": "platform", "env": "prod"}))

	for _, m := range []types.Map{out.AllowLabels, out.DenyLabels} {
		if m.IsNull() {
			t.Fatal("labels came back null")
		}
		if got := m.ElementType(context.Background()); !got.Equal(stateupgrade.LabelsV1Type) {
			t.Fatalf("element type is %v, want %v", got, stateupgrade.LabelsV1Type)
		}
		var got map[string][]string
		if d := m.ElementsAs(context.Background(), &got, false); d.HasError() {
			t.Fatalf("reading back: %v", d)
		}
		if len(got["team"]) != 1 || got["team"][0] != "platform" {
			t.Fatalf(`team = %v, want ["platform"]`, got["team"])
		}
		if len(got["env"]) != 1 || got["env"][0] != "prod" {
			t.Fatalf(`env = %v, want ["prod"]`, got["env"])
		}
	}
}

func TestNoLabelsStaysNullAtTheNEWElementType(t *testing.T) {
	// The COMMON case -- most roles carry no labels at all -- and the one a
	// careless upgrader breaks worse than the bug does: returning a null map
	// still typed map(string) carries v0's type into v1 state, and the
	// mismatch surfaces later on a role that never had labels.
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

func TestEveryOtherAttributePassesThroughUnchanged(t *testing.T) {
	out := upgrade(t, v0State(t, map[string]string{"team": "platform"}))

	for _, c := range []struct {
		name string
		got  string
		want string
	}{
		{"id", out.ID.ValueString(), "platform"},
		{"name", out.Name.ValueString(), "platform"},
		{"description", out.Description.ValueString(), "the one that broke"},
		{"workspace_permission", out.WorkspacePermission.ValueString(), "read"},
		{"created_at", out.CreatedAt.ValueString(), "2026-01-01T00:00:00Z"},
	} {
		if c.got != c.want {
			t.Errorf("%s = %q, want %q", c.name, c.got, c.want)
		}
	}
	if out.AllowAll.ValueBool() {
		t.Error("allow_all flipped")
	}
}

func TestTheResourceDeclaresTheUpgradeAtVersionZero(t *testing.T) {
	r := &roleResource{}
	upgraders := r.UpgradeState(context.Background())
	if _, ok := upgraders[0]; !ok {
		t.Fatalf("no upgrader from version 0; have %v", upgraders)
	}
	if got := currentSchema().Version; got != schemaVersion {
		t.Fatalf("schema version is %d, want %d", got, schemaVersion)
	}
	if schemaVersion == 0 {
		t.Fatal("a versioned schema cannot be version 0, or no upgrade is ever attempted")
	}
}

func TestThePriorSchemaStillDescribesVersionZero(t *testing.T) {
	// `priorSchemaV0` is DERIVED from the current schema, which is exact only
	// while the attribute SET has not moved since v1.11.4 -- measured, sixteen
	// either side. An attribute added later would otherwise be asserted to have
	// existed in v0 state that never carried it, and the decode would be
	// describing a shape that was never written.
	prior := priorSchemaV0()
	current := currentSchema()

	if len(prior.Attributes) != len(current.Attributes) {
		t.Fatalf("prior has %d attributes, current %d", len(prior.Attributes), len(current.Attributes))
	}
	for name := range current.Attributes {
		if _, ok := prior.Attributes[name]; !ok {
			t.Errorf("%q is in the current schema and not the prior one", name)
		}
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
