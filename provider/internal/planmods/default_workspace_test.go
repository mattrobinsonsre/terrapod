package planmods

import (
	"context"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/tfsdk"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-go/tftypes"
)

// workspaceSchema is the shape every resource this helper serves has in common:
// an Optional+Computed workspace_id beside something else.
func workspaceSchema() schema.Schema {
	return schema.Schema{
		Attributes: map[string]schema.Attribute{
			"workspace_id": schema.StringAttribute{Optional: true, Computed: true},
			"name":         schema.StringAttribute{Required: true},
		},
	}
}

func workspaceObject() tftypes.Object {
	return tftypes.Object{AttributeTypes: map[string]tftypes.Type{
		"workspace_id": tftypes.String,
		"name":         tftypes.String,
	}}
}

// runDefault drives the helper the way the framework does, with `planned` already
// holding whatever the attribute-level modifiers left behind.
//
// **Both inputs are explicit, because the defect in #2467 was that one of them
// was missing here.** The configuration and the plan are different values and
// the helper's whole job is to tell them apart: an omitted Optional+Computed
// attribute and one set to a not-yet-known reference BOTH arrive planned as
// unknown, so a harness that supplies only the plan cannot express the
// difference, and the case that shipped broken looked identical to the case
// that worked. Every test therefore states what the configuration said as well.
func runDefault(t *testing.T, configured, planned tftypes.Value, defaultWorkspace string) resource.ModifyPlanResponse {
	t.Helper()
	mk := func(ws tftypes.Value) tftypes.Value {
		return tftypes.NewValue(workspaceObject(), map[string]tftypes.Value{
			"workspace_id": ws,
			"name":         tftypes.NewValue(tftypes.String, "web-01"),
		})
	}
	plan := tfsdk.Plan{Schema: workspaceSchema(), Raw: mk(planned)}
	req := resource.ModifyPlanRequest{
		Config: tfsdk.Config{Schema: workspaceSchema(), Raw: mk(configured)},
		Plan:   plan,
	}
	resp := resource.ModifyPlanResponse{Plan: plan}
	DefaultWorkspaceID(context.Background(), req, &resp, defaultWorkspace)
	return resp
}

// The four values the two inputs take between them, named so each test says
// which of the four combinations it is.
var (
	absent  = tftypes.NewValue(tftypes.String, nil)
	pending = tftypes.NewValue(tftypes.String, tftypes.UnknownValue)
)

func plannedWorkspace(t *testing.T, resp resource.ModifyPlanResponse) types.String {
	t.Helper()
	var got types.String
	if d := resp.Plan.GetAttribute(context.Background(), path.Root("workspace_id"), &got); d.HasError() {
		t.Fatalf("reading the planned workspace_id: %v", d)
	}
	return got
}

func TestANullWorkspaceTakesTheDefault(t *testing.T) {
	resp := runDefault(t, absent, absent, "ws-default")
	if resp.Diagnostics.HasError() {
		t.Fatalf("unexpected error: %v", resp.Diagnostics)
	}
	if got := plannedWorkspace(t, resp); got.ValueString() != "ws-default" {
		t.Errorf("expected ws-default, got %q", got.ValueString())
	}
}

func TestAnOmittedWorkspacePlannedUnknownTakesTheDefault(t *testing.T) {
	// Optional+Computed with the configuration null arrives PLANNED as unknown
	// rather than null, which is the case that actually occurs on a create. This
	// is the default-workspace feature's main path (#2036) and the one a careless
	// reading of #2467 would break: the configuration is what must be null here,
	// not the plan.
	resp := runDefault(t, absent, pending, "ws-default")
	if resp.Diagnostics.HasError() {
		t.Fatalf("unexpected error: %v", resp.Diagnostics)
	}
	if got := plannedWorkspace(t, resp); got.ValueString() != "ws-default" {
		t.Errorf("expected ws-default, got %q", got.ValueString())
	}
}

func TestAConfiguredButBLANKWorkspaceIsNotTreatedAsNamed(t *testing.T) {
	// `workspace_id = var.ws` with `var.ws` unset arrives as a present, non-null
	// EMPTY STRING. `IsNull()` alone read that as "the practitioner named a
	// workspace", so the default was skipped AND the "Missing workspace"
	// diagnostic this modifier exists to raise was skipped too -- the apply then
	// posted to a path with an empty segment and the practitioner got a
	// server-side refusal in place of a plan-time error.
	//
	// With a default configured, the default applies.
	resp := runDefault(t, tftypes.NewValue(tftypes.String, ""), pending, "ws-default")
	if resp.Diagnostics.HasError() {
		t.Fatalf("unexpected error: %v", resp.Diagnostics)
	}
	if got := plannedWorkspace(t, resp); got.ValueString() != "ws-default" {
		t.Errorf("expected ws-default, got %q", got.ValueString())
	}
}

func TestABlankWorkspaceWithNoDefaultIsAPlanTimeError(t *testing.T) {
	// The other half: with no provider default there is nothing to fall back to,
	// so the blank must produce the diagnostic rather than an empty id on the
	// wire. Without this, returning early on blank would pass the test above.
	resp := runDefault(t, tftypes.NewValue(tftypes.String, ""), pending, "")
	if !resp.Diagnostics.HasError() {
		t.Fatal("a blank workspace_id with no default produced no diagnostic")
	}
}

func TestAConfiguredWorkspaceIsNeverOverwritten(t *testing.T) {
	// The whole point of the default being a fallback: declaring into another
	// workspace from inside a run has to keep working.
	resp := runDefault(t, tftypes.NewValue(tftypes.String, "ws-explicit"),
		tftypes.NewValue(tftypes.String, "ws-explicit"), "ws-default")
	if resp.Diagnostics.HasError() {
		t.Fatalf("unexpected error: %v", resp.Diagnostics)
	}
	if got := plannedWorkspace(t, resp); got.ValueString() != "ws-explicit" {
		t.Errorf("expected the configured ws-explicit to survive, got %q", got.ValueString())
	}
}

// The two tests below are #2467, and they are a pair on purpose: the attribute
// being unknown in the plan means two different things depending on what the
// configuration said, and only instantiating both states can tell them apart.
// The one above (an omitted attribute, planned unknown) must take the default;
// these two (a configured reference, planned unknown) must not be touched.

func TestAConfiguredButUnknownWorkspaceIsNeverRetargeted(t *testing.T) {
	// `workspace_id = terrapod_workspace.example.id` for a workspace created in
	// the same plan. This is the dangerous half of #2467 rather than the loud
	// one: a default IS available here, so nothing errored -- the reference was
	// quietly overwritten with it and the resource was planned into the wrong
	// workspace. Inside a Terrapod run TERRAPOD_WORKSPACE is always exported, so
	// a default is always available, so this was the common case.
	resp := runDefault(t, pending, pending, "ws-default")
	if resp.Diagnostics.HasError() {
		t.Fatalf("a configured reference must not error: %v", resp.Diagnostics)
	}
	got := plannedWorkspace(t, resp)
	if got.ValueString() == "ws-default" {
		t.Fatal("the configured reference was overwritten with the default workspace: " +
			"the resource would be planned into the wrong workspace, which is easy to " +
			"miss in a large plan (#2467)")
	}
	if !got.IsUnknown() {
		t.Errorf("the reference must stay unknown so the framework resolves it at apply, got %q", got)
	}
}

func TestAConfiguredButUnknownWorkspaceIsNotAMissingWorkspace(t *testing.T) {
	// The same configuration with no default available anywhere. This is the
	// reported symptom: a plan-time "Missing workspace" naming workspace_id as
	// unset, on a configuration whose very next line sets it.
	resp := runDefault(t, pending, pending, "")
	if resp.Diagnostics.HasError() {
		t.Fatalf("workspace_id IS set here -- a reference is not an omission: %v",
			resp.Diagnostics)
	}
	if got := plannedWorkspace(t, resp); !got.IsUnknown() {
		t.Errorf("expected the planned value to stay unknown, got %q", got)
	}
}

func TestAnExistingResourceKeepsTheWorkspaceStateCarriedOver(t *testing.T) {
	// The configuration omits workspace_id and UseStateForUnknown has already put
	// the existing value in the plan, so there is nothing to supply -- and
	// supplying one would move a live resource to a different workspace on a plan
	// that changed nothing. The default here deliberately DIFFERS from the carried
	// value, because a default equal to it is the shape that cannot fail.
	resp := runDefault(t, absent, tftypes.NewValue(tftypes.String, "ws-from-state"), "ws-other")
	if resp.Diagnostics.HasError() {
		t.Fatalf("unexpected error: %v", resp.Diagnostics)
	}
	if got := plannedWorkspace(t, resp); got.ValueString() != "ws-from-state" {
		t.Errorf("an existing resource was retargeted from ws-from-state to %q", got.ValueString())
	}
}

func TestNoWorkspaceAnywhereIsAPlanTimeError(t *testing.T) {
	// Relaxing Required costs the framework's own "argument is required" error.
	// Without this the omission would plan as "known after apply" and fail at
	// apply against the server with an empty id -- strictly worse than what it
	// replaced. The message must name all three sources.
	resp := runDefault(t, absent, absent, "")
	if !resp.Diagnostics.HasError() {
		t.Fatal("expected a plan-time error when no workspace is available anywhere")
	}
	joined := resp.Diagnostics.Errors()[0].Detail()
	for _, want := range []string{"workspace_id", "`workspace`", "TERRAPOD_WORKSPACE"} {
		if !strings.Contains(joined, want) {
			t.Errorf("the error should name %s; got %q", want, joined)
		}
	}
}

func TestDestroyIsLeftAlone(t *testing.T) {
	// A destroy's plan is null. Reading attributes off it would panic, and there
	// is nothing to default in any case.
	nullRaw := tftypes.NewValue(workspaceObject(), nil)
	plan := tfsdk.Plan{Schema: workspaceSchema(), Raw: nullRaw}
	req := resource.ModifyPlanRequest{
		// A destroy's config is null too, which is why the plan check has to come
		// first: reading an attribute off either would panic.
		Config: tfsdk.Config{Schema: workspaceSchema(), Raw: nullRaw},
		Plan:   plan,
	}
	resp := resource.ModifyPlanResponse{Plan: plan}
	DefaultWorkspaceID(context.Background(), req, &resp, "ws-default")
	if resp.Diagnostics.HasError() {
		t.Fatalf("a destroy must not error: %v", resp.Diagnostics)
	}
}

func TestAResourceWithoutTheAttributeIsNotOurBusiness(t *testing.T) {
	// The helper is called from resources that all have workspace_id today, but
	// it must not become the reason adding it elsewhere breaks.
	s := schema.Schema{Attributes: map[string]schema.Attribute{
		"name": schema.StringAttribute{Required: true},
	}}
	obj := tftypes.Object{AttributeTypes: map[string]tftypes.Type{"name": tftypes.String}}
	raw := tftypes.NewValue(obj, map[string]tftypes.Value{
		"name": tftypes.NewValue(tftypes.String, "web-01"),
	})
	plan := tfsdk.Plan{Schema: s, Raw: raw}
	req := resource.ModifyPlanRequest{
		// The guard reads the configuration first, so this is the value that has
		// to lack the attribute for the early return to be the one under test.
		Config: tfsdk.Config{Schema: s, Raw: raw},
		Plan:   plan,
	}
	resp := resource.ModifyPlanResponse{Plan: plan}
	DefaultWorkspaceID(context.Background(), req, &resp, "ws-default")
	if resp.Diagnostics.HasError() {
		t.Fatalf("a resource without workspace_id must be ignored: %v", resp.Diagnostics)
	}
}

// The two helpers compose, and the order matters. KeepComputedWhenUnchanged
// treats an unknown planned value as a change ("An unknown planned value is
// never Equal to a known one"), so a workspace_id still unknown by the time it
// runs makes a no-change re-plan look like an update and the carried computed
// attributes go unknown for ever -- the #1619 shape, which this project has
// already shipped once.
//
// terrapod_variable runs DefaultWorkspaceID first for exactly this reason. This
// drives the same pair in the same order over a no-change re-plan.
func TestTheDefaultRunsBeforeKeepComputedOrTheReplanNeverConverges(t *testing.T) {
	s := schema.Schema{Attributes: map[string]schema.Attribute{
		"workspace_id": schema.StringAttribute{Optional: true, Computed: true},
		"key":          schema.StringAttribute{Required: true},
		"updated_at":   schema.StringAttribute{Computed: true},
	}}
	obj := tftypes.Object{AttributeTypes: map[string]tftypes.Type{
		"workspace_id": tftypes.String,
		"key":          tftypes.String,
		"updated_at":   tftypes.String,
	}}
	mk := func(ws, updated tftypes.Value) tftypes.Value {
		return tftypes.NewValue(obj, map[string]tftypes.Value{
			"workspace_id": ws,
			"key":          tftypes.NewValue(tftypes.String, "DB_HOST"),
			"updated_at":   updated,
		})
	}
	// State holds the real values; the plan arrives with workspace_id unknown
	// (config omits it) and updated_at unknown (computed).
	state := tfsdk.State{Schema: s, Raw: mk(
		tftypes.NewValue(tftypes.String, "ws-default"),
		tftypes.NewValue(tftypes.String, "2026-10-12T00:00:00Z"))}
	plan := tfsdk.Plan{Schema: s, Raw: mk(
		tftypes.NewValue(tftypes.String, tftypes.UnknownValue),
		tftypes.NewValue(tftypes.String, tftypes.UnknownValue))}

	// The configuration omits workspace_id -- that is what makes the default
	// apply at all, and it is the input the planned unknown cannot express.
	config := tfsdk.Config{Schema: s, Raw: mk(
		tftypes.NewValue(tftypes.String, nil),
		tftypes.NewValue(tftypes.String, nil))}

	req := resource.ModifyPlanRequest{Config: config, Plan: plan, State: state}
	resp := resource.ModifyPlanResponse{Plan: plan}

	ctx := context.Background()
	DefaultWorkspaceID(ctx, req, &resp, "ws-default")
	KeepComputedWhenUnchanged(ctx, req, &resp, []string{"workspace_id", "key"}, []string{"updated_at"})

	if resp.Diagnostics.HasError() {
		t.Fatalf("unexpected error: %v", resp.Diagnostics)
	}
	var updated types.String
	if d := resp.Plan.GetAttribute(ctx, path.Root("updated_at"), &updated); d.HasError() {
		t.Fatalf("reading updated_at: %v", d)
	}
	if updated.IsUnknown() {
		t.Fatal("updated_at stayed unknown on a no-change re-plan: the default did not " +
			"reach KeepComputedWhenUnchanged, so every plan will show an update that " +
			"never converges (#1619)")
	}
	if got := updated.ValueString(); got != "2026-10-12T00:00:00Z" {
		t.Errorf("updated_at should have been carried from state, got %q", got)
	}
}
