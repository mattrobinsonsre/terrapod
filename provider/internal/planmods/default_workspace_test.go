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
func runDefault(t *testing.T, planned tftypes.Value, defaultWorkspace string) resource.ModifyPlanResponse {
	t.Helper()
	raw := tftypes.NewValue(workspaceObject(), map[string]tftypes.Value{
		"workspace_id": planned,
		"name":         tftypes.NewValue(tftypes.String, "web-01"),
	})
	plan := tfsdk.Plan{Schema: workspaceSchema(), Raw: raw}
	req := resource.ModifyPlanRequest{Plan: plan}
	resp := resource.ModifyPlanResponse{Plan: plan}
	DefaultWorkspaceID(context.Background(), req, &resp, defaultWorkspace)
	return resp
}

func plannedWorkspace(t *testing.T, resp resource.ModifyPlanResponse) types.String {
	t.Helper()
	var got types.String
	if d := resp.Plan.GetAttribute(context.Background(), path.Root("workspace_id"), &got); d.HasError() {
		t.Fatalf("reading the planned workspace_id: %v", d)
	}
	return got
}

func TestANullWorkspaceTakesTheDefault(t *testing.T) {
	resp := runDefault(t, tftypes.NewValue(tftypes.String, nil), "ws-default")
	if resp.Diagnostics.HasError() {
		t.Fatalf("unexpected error: %v", resp.Diagnostics)
	}
	if got := plannedWorkspace(t, resp); got.ValueString() != "ws-default" {
		t.Errorf("expected ws-default, got %q", got.ValueString())
	}
}

func TestAnUnknownWorkspaceTakesTheDefault(t *testing.T) {
	// Optional+Computed with the configuration null arrives here unknown rather
	// than null, which is the case that actually occurs on a create.
	resp := runDefault(t, tftypes.NewValue(tftypes.String, tftypes.UnknownValue), "ws-default")
	if resp.Diagnostics.HasError() {
		t.Fatalf("unexpected error: %v", resp.Diagnostics)
	}
	if got := plannedWorkspace(t, resp); got.ValueString() != "ws-default" {
		t.Errorf("expected ws-default, got %q", got.ValueString())
	}
}

func TestAConfiguredWorkspaceIsNeverOverwritten(t *testing.T) {
	// The whole point of the default being a fallback: declaring into another
	// workspace from inside a run has to keep working.
	resp := runDefault(t, tftypes.NewValue(tftypes.String, "ws-explicit"), "ws-default")
	if resp.Diagnostics.HasError() {
		t.Fatalf("unexpected error: %v", resp.Diagnostics)
	}
	if got := plannedWorkspace(t, resp); got.ValueString() != "ws-explicit" {
		t.Errorf("expected the configured ws-explicit to survive, got %q", got.ValueString())
	}
}

func TestNoWorkspaceAnywhereIsAPlanTimeError(t *testing.T) {
	// Relaxing Required costs the framework's own "argument is required" error.
	// Without this the omission would plan as "known after apply" and fail at
	// apply against the server with an empty id -- strictly worse than what it
	// replaced. The message must name all three sources.
	resp := runDefault(t, tftypes.NewValue(tftypes.String, nil), "")
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
	plan := tfsdk.Plan{Schema: workspaceSchema(), Raw: tftypes.NewValue(workspaceObject(), nil)}
	req := resource.ModifyPlanRequest{Plan: plan}
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
	req := resource.ModifyPlanRequest{Plan: plan}
	resp := resource.ModifyPlanResponse{Plan: plan}
	DefaultWorkspaceID(context.Background(), req, &resp, "ws-default")
	if resp.Diagnostics.HasError() {
		t.Fatalf("a resource without workspace_id must be ignored: %v", resp.Diagnostics)
	}
}
