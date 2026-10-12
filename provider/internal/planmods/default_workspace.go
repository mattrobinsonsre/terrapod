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
//
// **With no workspace from any source it raises a plan-time error**, which is the
// point rather than a nicety. Making a Required attribute Optional costs the
// framework's own "argument is required" error, and without this the omission
// would instead plan as "known after apply" and fail at apply against the server
// with an empty id -- strictly worse than what it replaced. This names all three
// sources, so it is better than what it replaced rather than merely as good.
func DefaultWorkspaceID(
	ctx context.Context,
	req resource.ModifyPlanRequest,
	resp *resource.ModifyPlanResponse,
	defaultWorkspace string,
) {
	// Destroy has no plan to fill.
	if req.Plan.Raw.IsNull() {
		return
	}
	var planned types.String
	if d := resp.Plan.GetAttribute(ctx, path.Root("workspace_id"), &planned); d.HasError() {
		// A resource without the attribute is not this helper's business.
		return
	}
	// Already named, or carried over from state by UseStateForUnknown (whose
	// attribute-level modifier has run by now), so there is nothing to supply.
	if !planned.IsNull() && !planned.IsUnknown() {
		return
	}
	if defaultWorkspace == "" {
		resp.Diagnostics.AddError("Missing workspace",
			"Set workspace_id on this resource, or the provider's `workspace` attribute, or "+
				"TERRAPOD_WORKSPACE. Inside a Terrapod run the last is exported for you.")
		return
	}
	resp.Diagnostics.Append(
		resp.Plan.SetAttribute(ctx, path.Root("workspace_id"), types.StringValue(defaultWorkspace))...)
}
