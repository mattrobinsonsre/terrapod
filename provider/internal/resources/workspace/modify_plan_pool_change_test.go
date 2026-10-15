package workspace

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

// Drives the real ModifyPlan, which the helper-level tests cannot: a gate over
// `derivedNames` is satisfied by the ledger alone, so deleting the
// `UnknownStringWhenSourceChanges` call would leave every other test in this
// package green. The same is true of the warning -- `poolSetSize` can be
// correct while the branch still warns on a 1->1 move.
func wsSchema(t *testing.T) schema.Schema {
	t.Helper()
	var r workspaceResource
	resp := &resource.SchemaResponse{}
	r.Schema(context.Background(), resource.SchemaRequest{}, resp)
	return schema.Schema(resp.Schema)
}

// wsValue builds a whole-resource value with every attribute null except the
// ones given, so a test states only what it is about.
func wsValue(t *testing.T, s schema.Schema, set map[string]tftypes.Value) tftypes.Value {
	t.Helper()
	obj, ok := s.Type().TerraformType(context.Background()).(tftypes.Object)
	if !ok {
		t.Fatal("the workspace schema is not an object type")
	}
	vals := map[string]tftypes.Value{}
	for name, ty := range obj.AttributeTypes {
		if v, found := set[name]; found {
			vals[name] = v
			continue
		}
		vals[name] = tftypes.NewValue(ty, nil)
	}
	return tftypes.NewValue(obj, vals)
}

func strListVal(t *testing.T, s schema.Schema, vals ...string) tftypes.Value {
	t.Helper()
	elems := make([]tftypes.Value, 0, len(vals))
	for _, v := range vals {
		elems = append(elems, tftypes.NewValue(tftypes.String, v))
	}
	return tftypes.NewValue(tftypes.List{ElementType: tftypes.String}, elems)
}

// runModifyPlan drives it with the plan already holding what the attribute-level
// modifiers left behind -- which for `agent_pool_name` is the PRIOR name,
// because UseStateForUnknown put it there. That is the setup of #2110.
func runModifyPlan(t *testing.T, priorPools []string, priorName, cfgPoolID string) resource.ModifyPlanResponse {
	t.Helper()
	s := wsSchema(t)

	state := wsValue(t, s, map[string]tftypes.Value{
		"id":              tftypes.NewValue(tftypes.String, "ws-1"),
		"name":            tftypes.NewValue(tftypes.String, "argocd"),
		"agent_pool_ids":  strListVal(t, s, priorPools...),
		"agent_pool_id":   tftypes.NewValue(tftypes.String, priorPools[0]),
		"agent_pool_name": tftypes.NewValue(tftypes.String, priorName),
	})
	// The config names only the singular, which is the shape in the report.
	cfg := wsValue(t, s, map[string]tftypes.Value{
		"name":          tftypes.NewValue(tftypes.String, "argocd"),
		"agent_pool_id": tftypes.NewValue(tftypes.String, cfgPoolID),
	})
	plan := wsValue(t, s, map[string]tftypes.Value{
		"id":            tftypes.NewValue(tftypes.String, "ws-1"),
		"name":          tftypes.NewValue(tftypes.String, "argocd"),
		"agent_pool_id": tftypes.NewValue(tftypes.String, cfgPoolID),
		// UseStateForUnknown carried both of these forward from state.
		"agent_pool_ids":  strListVal(t, s, priorPools...),
		"agent_pool_name": tftypes.NewValue(tftypes.String, priorName),
	})

	var r workspaceResource
	req := resource.ModifyPlanRequest{
		State:  tfsdk.State{Schema: s, Raw: state},
		Config: tfsdk.Config{Schema: s, Raw: cfg},
		Plan:   tfsdk.Plan{Schema: s, Raw: plan},
	}
	resp := resource.ModifyPlanResponse{Plan: tfsdk.Plan{Schema: s, Raw: plan}}
	r.ModifyPlan(context.Background(), req, &resp)
	return resp
}

func warningsOf(resp resource.ModifyPlanResponse) []string {
	var out []string
	for _, d := range resp.Diagnostics.Warnings() {
		out = append(out, d.Summary())
	}
	return out
}

func TestAOneToOnePoolChangeNeitherWarnsNorGoesStale(t *testing.T) {
	// The reported case: one pool, moved to another, config using the singular.
	resp := runModifyPlan(t, []string{"apool-a"}, "pool-a-name", "apool-b")
	if resp.Diagnostics.HasError() {
		t.Fatalf("unexpected error: %v", resp.Diagnostics)
	}

	for _, w := range warningsOf(resp) {
		if strings.Contains(w, "drop the workspace's other agent pools") {
			t.Error("warned that the workspace runs on several agent pools; it ran on " +
				"exactly one, and a 1->1 move drops nothing (#2110)")
		}
	}

	var name types.String
	if d := resp.Plan.GetAttribute(context.Background(), path.Root("agent_pool_name"), &name); d.HasError() {
		t.Fatalf("reading agent_pool_name: %v", d)
	}
	if !name.IsUnknown() {
		t.Errorf("agent_pool_name planned as %v, want unknown — the plan promises the old "+
			"name and the apply returns the new one, which the framework refuses as an "+
			"inconsistent result AFTER the pool has already moved (#2110)", name)
	}

	// Still marked unknown, because the set genuinely changes either way.
	var ids types.List
	if d := resp.Plan.GetAttribute(context.Background(), path.Root("agent_pool_ids"), &ids); d.HasError() {
		t.Fatalf("reading agent_pool_ids: %v", d)
	}
	if !ids.IsUnknown() {
		t.Error("agent_pool_ids must still be unknown on a pool change (#1094's half)")
	}
}

func TestAManyToOnePoolChangeStillWarns(t *testing.T) {
	// The case the warning exists for (#1094). Narrowing it must not retire it.
	resp := runModifyPlan(t, []string{"apool-a", "apool-b"}, "pool-a-name", "apool-c")
	found := false
	for _, w := range warningsOf(resp) {
		if strings.Contains(w, "drop the workspace's other agent pools") {
			found = true
		}
	}
	if !found {
		t.Errorf("no drop warning for a 2->1 change; the singular replaces the whole set "+
			"and one pool is genuinely lost. Warnings: %v", warningsOf(resp))
	}
}

func TestANoChangeReplanKeepsTheNameKnown(t *testing.T) {
	// The other direction: marking the name unknown unconditionally would show
	// a diff on every plan for a value nothing is moving.
	resp := runModifyPlan(t, []string{"apool-a"}, "pool-a-name", "apool-a")
	var name types.String
	if d := resp.Plan.GetAttribute(context.Background(), path.Root("agent_pool_name"), &name); d.HasError() {
		t.Fatalf("reading agent_pool_name: %v", d)
	}
	if name.IsUnknown() {
		t.Error("agent_pool_name went unknown on a no-change re-plan — a perpetual diff")
	}
	if name.ValueString() != "pool-a-name" {
		t.Errorf("agent_pool_name = %q, want it carried from state", name.ValueString())
	}
	for _, w := range warningsOf(resp) {
		if strings.Contains(w, "drop the workspace's other agent pools") {
			t.Error("warned on a no-change re-plan")
		}
	}
}
