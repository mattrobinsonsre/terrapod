package planmods

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/tfsdk"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-go/tftypes"
)

// The shape this helper exists for: a Computed name the server derives from a
// configurable id.
func derivedSchema() schema.Schema {
	return schema.Schema{
		Attributes: map[string]schema.Attribute{
			"pool_id":   schema.StringAttribute{Optional: true, Computed: true},
			"pool_name": schema.StringAttribute{Computed: true},
		},
	}
}

func derivedObject() tftypes.Object {
	return tftypes.Object{AttributeTypes: map[string]tftypes.Type{
		"pool_id":   tftypes.String,
		"pool_name": tftypes.String,
	}}
}

// runDerived drives the helper the way the framework does: `planned` is what the
// attribute-level modifiers left behind, so `pool_name` arrives carrying the
// PRIOR name because UseStateForUnknown put it there. That is the whole setup
// of #2110 -- a plan promising the old name against an apply that returns the
// new one.
func runDerived(t *testing.T, priorID, priorName, plannedID, plannedName tftypes.Value, destroy bool) resource.ModifyPlanResponse {
	t.Helper()
	mk := func(id, name tftypes.Value) tftypes.Value {
		return tftypes.NewValue(derivedObject(), map[string]tftypes.Value{
			"pool_id": id, "pool_name": name,
		})
	}
	state := tfsdk.State{Schema: derivedSchema(), Raw: mk(priorID, priorName)}
	planRaw := mk(plannedID, plannedName)
	if destroy {
		planRaw = tftypes.NewValue(derivedObject(), nil)
	}
	plan := tfsdk.Plan{Schema: derivedSchema(), Raw: planRaw}
	req := resource.ModifyPlanRequest{State: state, Plan: plan}
	resp := resource.ModifyPlanResponse{Plan: plan}
	UnknownStringWhenSourceChanges(context.Background(), req, &resp,
		[]Derived{{Name: "pool_name", Sources: []string{"pool_id"}}})
	return resp
}

func plannedName(t *testing.T, resp resource.ModifyPlanResponse) types.String {
	t.Helper()
	var got types.String
	if d := resp.Plan.GetAttribute(context.Background(), path.Root("pool_name"), &got); d.HasError() {
		t.Fatalf("reading the planned pool_name: %v", d)
	}
	return got
}

func s(v string) tftypes.Value { return tftypes.NewValue(tftypes.String, v) }

func TestAChangedSourceMakesTheDerivedNameUnknown(t *testing.T) {
	// The reported bug (#2110). Without this the plan promises "mgmt-azure"
	// and the apply returns "mgmt", which the framework refuses as an
	// inconsistent result -- AFTER the server-side move has succeeded.
	resp := runDerived(t, s("apool-a"), s("mgmt-azure"), s("apool-b"), s("mgmt-azure"), false)
	if resp.Diagnostics.HasError() {
		t.Fatalf("unexpected error: %v", resp.Diagnostics)
	}
	if got := plannedName(t, resp); !got.IsUnknown() {
		t.Errorf("pool_name planned as %v, want unknown — a 1->1 id change leaves the "+
			"derived name stale and the apply fails on an inconsistent result", got)
	}
}

func TestAnUnchangedSourceLeavesTheDerivedNameAlone(t *testing.T) {
	// The other half, and the reason this is not just "always unknown": on a
	// no-change re-plan the name must stay known, or every plan shows a
	// perpetual diff on an attribute nothing is moving.
	resp := runDerived(t, s("apool-a"), s("mgmt-azure"), s("apool-a"), s("mgmt-azure"), false)
	got := plannedName(t, resp)
	if got.IsUnknown() {
		t.Error("pool_name went unknown with its source unchanged — that is a diff on " +
			"every plan for a value nothing is changing")
	}
	if got.ValueString() != "mgmt-azure" {
		t.Errorf("pool_name = %q, want %q", got.ValueString(), "mgmt-azure")
	}
}

func TestAnUnknownSourceCountsAsAChange(t *testing.T) {
	// A source still being computed upstream cannot promise anything about the
	// name, so the conservative direction is unknown. Same reasoning as
	// KeepComputedWhenUnchanged's own unknown handling.
	resp := runDerived(t, s("apool-a"), s("mgmt-azure"),
		tftypes.NewValue(tftypes.String, tftypes.UnknownValue), s("mgmt-azure"), false)
	if got := plannedName(t, resp); !got.IsUnknown() {
		t.Errorf("pool_name planned as %v, want unknown for an unknown source", got)
	}
}

func TestCreateAndDestroyAreLeftAlone(t *testing.T) {
	// Create: no prior state, so there is no stale value to correct, and
	// touching the plan would be inventing one.
	mk := func(id, name tftypes.Value) tftypes.Value {
		return tftypes.NewValue(derivedObject(), map[string]tftypes.Value{
			"pool_id": id, "pool_name": name,
		})
	}
	plan := tfsdk.Plan{Schema: derivedSchema(), Raw: mk(s("apool-b"), tftypes.NewValue(tftypes.String, tftypes.UnknownValue))}
	req := resource.ModifyPlanRequest{
		State: tfsdk.State{Schema: derivedSchema(), Raw: tftypes.NewValue(derivedObject(), nil)},
		Plan:  plan,
	}
	resp := resource.ModifyPlanResponse{Plan: plan}
	UnknownStringWhenSourceChanges(context.Background(), req, &resp,
		[]Derived{{Name: "pool_name", Sources: []string{"pool_id"}}})
	if resp.Diagnostics.HasError() {
		t.Fatalf("create raised: %v", resp.Diagnostics)
	}

	// Destroy: a null plan. Writing into it is how a helper corrupts a destroy.
	resp2 := runDerived(t, s("apool-a"), s("mgmt-azure"), s("apool-b"), s("mgmt-azure"), true)
	if resp2.Diagnostics.HasError() {
		t.Fatalf("destroy raised: %v", resp2.Diagnostics)
	}
	if !resp2.Plan.Raw.IsNull() {
		t.Error("the destroy plan was written into; it must be left null")
	}
}

func TestAnAttributeNotInTheSchemaIsNotOurBusiness(t *testing.T) {
	// A Derived entry naming something this resource does not have must be a
	// no-op rather than an error, so one shared list can serve several
	// resources without each needing its own.
	resp := runDerived(t, s("apool-a"), s("mgmt-azure"), s("apool-b"), s("mgmt-azure"), false)
	_ = resp
	mk := func(id, name tftypes.Value) tftypes.Value {
		return tftypes.NewValue(derivedObject(), map[string]tftypes.Value{
			"pool_id": id, "pool_name": name,
		})
	}
	plan := tfsdk.Plan{Schema: derivedSchema(), Raw: mk(s("apool-b"), s("mgmt-azure"))}
	req := resource.ModifyPlanRequest{
		State: tfsdk.State{Schema: derivedSchema(), Raw: mk(s("apool-a"), s("mgmt-azure"))},
		Plan:  plan,
	}
	out := resource.ModifyPlanResponse{Plan: plan}
	UnknownStringWhenSourceChanges(context.Background(), req, &out,
		[]Derived{{Name: "pool_name", Sources: []string{"not_an_attribute"}}})
	if out.Diagnostics.HasError() {
		t.Fatalf("an absent source raised: %v", out.Diagnostics)
	}
	if plannedName(t, out).IsUnknown() {
		t.Error("an absent source marked the name unknown; it carries no information")
	}
}
