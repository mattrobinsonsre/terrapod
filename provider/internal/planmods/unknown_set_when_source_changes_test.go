package planmods

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/tfsdk"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-go/tftypes"
)

// The shape this modifier exists for, as `terrapod_role` has it: an
// Optional+Computed set the server derives from an Optional+Computed level.
func derivedSetSchema() schema.Schema {
	return schema.Schema{
		Attributes: map[string]schema.Attribute{
			"workspace_permission": schema.StringAttribute{Optional: true, Computed: true},
			"description":          schema.StringAttribute{Optional: true},
			"capabilities": schema.SetAttribute{
				Optional: true, Computed: true, ElementType: types.StringType,
			},
		},
	}
}

func derivedSetObject() tftypes.Object {
	return tftypes.Object{AttributeTypes: map[string]tftypes.Type{
		"workspace_permission": tftypes.String,
		"description":          tftypes.String,
		"capabilities":         tftypes.Set{ElementType: tftypes.String},
	}}
}

var (
	nullLevel = tftypes.NewValue(tftypes.String, nil)
	nullCaps  = tftypes.NewValue(tftypes.Set{ElementType: tftypes.String}, nil)
)

func lvl(s string) tftypes.Value { return tftypes.NewValue(tftypes.String, s) }

func rawCaps(items ...string) tftypes.Value {
	elems := make([]tftypes.Value, 0, len(items))
	for _, i := range items {
		elems = append(elems, tftypes.NewValue(tftypes.String, i))
	}
	return tftypes.NewValue(tftypes.Set{ElementType: tftypes.String}, elems)
}

func capsSet(t *testing.T, items ...string) types.Set {
	t.Helper()
	out, diags := types.SetValueFrom(context.Background(), types.StringType, items)
	if diags.HasError() {
		t.Fatalf("building a set: %v", diags)
	}
	return out
}

// setCase is one framework-shaped invocation. Only the inputs the modifier
// reads are named, so a case states exactly what it is about.
type setCase struct {
	// cfgLevel is the CONFIGURATION's level -- the input the modifier keys on.
	// Null means the practitioner did not author it, so what state holds is a
	// server-derived summary.
	cfgLevel tftypes.Value
	// priorLevel and priorCaps are what state holds.
	priorLevel tftypes.Value
	priorCaps  []string
	// authored, when non-nil, is a `capabilities` set present in configuration.
	authored []string
	create   bool
	destroy  bool
}

// run drives the modifier the way the framework does. For an Optional+Computed
// attribute the framework resolves the planned value BEFORE attribute modifiers
// run: the configured set when there is one, otherwise unknown. Both are
// modelled, because the authored case is the one that must be left alone.
func (c setCase) run(t *testing.T) planmodifier.SetResponse {
	t.Helper()

	cfgCaps := types.SetNull(types.StringType)
	cfgCapsRaw := nullCaps
	planValue := types.SetUnknown(types.StringType)
	if c.authored != nil {
		cfgCaps = capsSet(t, c.authored...)
		cfgCapsRaw = rawCaps(c.authored...)
		planValue = cfgCaps
	}

	mk := func(level, caps tftypes.Value) tftypes.Value {
		return tftypes.NewValue(derivedSetObject(), map[string]tftypes.Value{
			"workspace_permission": level,
			"description":          tftypes.NewValue(tftypes.String, nil),
			"capabilities":         caps,
		})
	}

	stateRaw := mk(c.priorLevel, rawCaps(c.priorCaps...))
	stateValue := capsSet(t, c.priorCaps...)
	if c.create {
		stateRaw = tftypes.NewValue(derivedSetObject(), nil)
		stateValue = types.SetNull(types.StringType)
	}
	planRaw := mk(c.cfgLevel, cfgCapsRaw)
	if c.destroy {
		planRaw = tftypes.NewValue(derivedSetObject(), nil)
	}

	req := planmodifier.SetRequest{
		Path:        path.Root("capabilities"),
		Config:      tfsdk.Config{Schema: derivedSetSchema(), Raw: mk(c.cfgLevel, cfgCapsRaw)},
		ConfigValue: cfgCaps,
		Plan:        tfsdk.Plan{Schema: derivedSetSchema(), Raw: planRaw},
		PlanValue:   planValue,
		State:       tfsdk.State{Schema: derivedSetSchema(), Raw: stateRaw},
		StateValue:  stateValue,
	}
	resp := planmodifier.SetResponse{PlanValue: req.PlanValue}
	UnknownSetWhenSourceChanges("workspace_permission").PlanModifySet(
		context.Background(), req, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("unexpected error diagnostics: %v", resp.Diagnostics)
	}
	return resp
}

// THE DEFECT (#2502). The configuration moves the level, so the prior derived
// set is stale. Carrying it forward is what made the provider send it back, the
// server store it as authored and drop the level edit -- ending in "Provider
// produced inconsistent result after apply" on a role that could not converge,
// which blocked every later apply of the configuration.
func TestAChangedConfiguredLevelLeavesTheSetUnknown(t *testing.T) {
	resp := setCase{
		cfgLevel: lvl("write"), priorLevel: lvl("custom"),
		priorCaps: []string{"workspace:read"},
	}.run(t)
	if !resp.PlanValue.IsUnknown() {
		t.Fatalf("a changed level must leave capabilities unknown for the server to "+
			"re-derive, got %v", resp.PlanValue)
	}
}

// The other half, and why this is not simply "always unknown": an edit to
// something else must not turn the set into "known after apply" on every plan
// (the #1619 never-converging-plan-noise class).
func TestAnUnchangedConfiguredLevelCarriesThePriorSetForward(t *testing.T) {
	resp := setCase{
		cfgLevel: lvl("write"), priorLevel: lvl("write"),
		priorCaps: []string{"run:plan", "workspace:read"},
	}.run(t)
	if resp.PlanValue.IsUnknown() {
		t.Fatalf("an unchanged level must keep the prior set, not plan it unknown")
	}
	want := capsSet(t, "run:plan", "workspace:read")
	if !resp.PlanValue.Equal(want) {
		t.Errorf("planned %v, want the prior set %v", resp.PlanValue, want)
	}
}

// The source is Optional+Computed, so an unauthored level holds the SERVER's
// own derived summary. A summary is not an instruction, so it cannot be what
// makes the set stale -- and reading it as one would force the set unknown on
// every plan of a capability-authored role, which is plan noise that never
// settles.
func TestAnUnauthoredLevelIsNotAChangeHoweverFarItHasMoved(t *testing.T) {
	resp := setCase{
		cfgLevel: nullLevel, priorLevel: lvl("custom"),
		priorCaps: []string{"workspace:read"},
	}.run(t)
	if resp.PlanValue.IsUnknown() {
		t.Fatalf("a level absent from configuration is a derived summary; it must not " +
			"force the set unknown")
	}
	if !resp.PlanValue.Equal(capsSet(t, "workspace:read")) {
		t.Errorf("planned %v, want the prior set carried forward", resp.PlanValue)
	}
}

// An authored set is the stored truth and the levels are its summary, so there
// is no derivation to invalidate. This is what keeps the issue's "authored
// capabilities is unchanged" true: the configured value survives even though
// the level moved, which is the one case that would otherwise be destroyed.
func TestAnAuthoredSetSurvivesALevelChange(t *testing.T) {
	resp := setCase{
		cfgLevel: lvl("write"), priorLevel: lvl("custom"),
		priorCaps: []string{"workspace:read"},
		authored:  []string{"workspace:admin"},
	}.run(t)
	want := capsSet(t, "workspace:admin")
	if !resp.PlanValue.Equal(want) {
		t.Fatalf("an authored set must be left exactly as configured; planned %v, want %v",
			resp.PlanValue, want)
	}
}

// An unknown configured level counts as a change: whatever it resolves to may
// differ, so the prior set cannot be promised.
func TestAnUnknownConfiguredLevelCountsAsAChange(t *testing.T) {
	resp := setCase{
		cfgLevel:   tftypes.NewValue(tftypes.String, tftypes.UnknownValue),
		priorLevel: lvl("write"), priorCaps: []string{"workspace:read"},
	}.run(t)
	if !resp.PlanValue.IsUnknown() {
		t.Fatalf("an unknown level must leave the set unknown, got %v", resp.PlanValue)
	}
}

// Create has no prior value to carry and a destroy plans nothing; neither may
// panic or invent one.
func TestCreateAndDestroyLeaveTheSetAlone(t *testing.T) {
	create := setCase{cfgLevel: lvl("write"), priorLevel: nullLevel, create: true}.run(t)
	if !create.PlanValue.IsUnknown() {
		t.Errorf("create has nothing to carry forward, so the set stays unknown; got %v",
			create.PlanValue)
	}
	destroy := setCase{
		cfgLevel: lvl("write"), priorLevel: lvl("write"),
		priorCaps: []string{"workspace:read"}, destroy: true,
	}.run(t)
	if !destroy.PlanValue.IsUnknown() {
		t.Errorf("a destroy plans nothing, so the set is left as it arrived; got %v",
			destroy.PlanValue)
	}
}

// A source that is not an attribute of the resource is skipped rather than
// panicking -- the same tolerance `UnknownStringWhenSourceChanges` has, and the
// reason the role resource can name its four levels without the modifier
// needing to know the schema.
func TestAnUnknownSourceNameIsSkipped(t *testing.T) {
	raw := tftypes.NewValue(derivedSetObject(), map[string]tftypes.Value{
		"workspace_permission": lvl("write"),
		"description":          tftypes.NewValue(tftypes.String, nil),
		"capabilities":         rawCaps("workspace:read"),
	})
	req := planmodifier.SetRequest{
		Path:        path.Root("capabilities"),
		Config:      tfsdk.Config{Schema: derivedSetSchema(), Raw: raw},
		ConfigValue: types.SetNull(types.StringType),
		Plan:        tfsdk.Plan{Schema: derivedSetSchema(), Raw: raw},
		PlanValue:   types.SetUnknown(types.StringType),
		State:       tfsdk.State{Schema: derivedSetSchema(), Raw: raw},
		StateValue:  capsSet(t, "workspace:read"),
	}
	resp := planmodifier.SetResponse{PlanValue: req.PlanValue}
	UnknownSetWhenSourceChanges("not_an_attribute").PlanModifySet(
		context.Background(), req, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("unexpected diagnostics: %v", resp.Diagnostics)
	}
	if resp.PlanValue.IsUnknown() {
		t.Errorf("a source that is not an attribute must be skipped, not treated as changed")
	}
}
