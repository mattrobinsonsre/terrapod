package planmods

import (
	"context"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// DefaultWorkspaceID fills a `workspace_id` the configuration left out from the
// provider's default, during ModifyPlan (#2036).
//
// **Why ModifyPlan and not Create.** The attribute is Optional+Computed, so with
// the configuration null the framework leaves it unknown on a create. Resolving
// it in Create would set state to a value the plan did not contain, which is
// exactly "Provider produced inconsistent result after apply". Filling it here
// puts the value in the plan, so the practitioner also sees which workspace the
// plan is about before applying it — which is the better half of doing it here.
//
// A configured value always wins, and an existing resource keeps its state value
// through UseStateForUnknown, so this only ever supplies a value nobody named.
// With no default available it does nothing and the resource's own Create fails
// with the server's error, which names the missing field.
func DefaultWorkspaceID(
	ctx context.Context,
	req resource.ModifyPlanRequest,
	resp *resource.ModifyPlanResponse,
	defaultWorkspace string,
) {
	// Destroy has no plan to fill.
	if req.Plan.Raw.IsNull() || defaultWorkspace == "" {
		return
	}
	var planned types.String
	if d := resp.Plan.GetAttribute(ctx, path.Root("workspace_id"), &planned); d.HasError() {
		// A resource without the attribute is not this helper's business.
		return
	}
	if !planned.IsNull() && !planned.IsUnknown() {
		return
	}
	resp.Diagnostics.Append(
		resp.Plan.SetAttribute(ctx, path.Root("workspace_id"), types.StringValue(defaultWorkspace))...)
}
