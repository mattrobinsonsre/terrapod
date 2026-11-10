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
// **It asks the CONFIGURATION whether the attribute was left out, not the plan
// (#2467).** Those are different questions and the plan cannot tell them apart:
// the attribute is Optional+Computed, so a configuration that omits it plans as
// unknown -- and so does a configuration that sets it to a reference whose value
// is not known yet, such as a workspace created in the same plan. Deciding from
// the planned value alone therefore read `workspace_id = terrapod_workspace.x.id`
// as an omission, and either raised "Missing workspace" on a configuration that
// names one, or -- with a default available, which inside a Terrapod run it
// always is -- silently overwrote the reference with the run's own workspace and
// sent the resource to the wrong place. The configured value is the only input
// that distinguishes the two, so it is the one consulted first.
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
	var configured types.String
	if d := req.Config.GetAttribute(ctx, path.Root("workspace_id"), &configured); d.HasError() {
		// A resource without the attribute is not this helper's business.
		return
	}
	// Named in the configuration. Whether that value is known yet is none of our
	// business -- an unknown here is a reference to something being created in
	// this same plan, and it has to stay unknown so the framework resolves it at
	// apply. Filling a default over it would retarget the resource.
	// An EMPTY STRING is not "named". `IsNull()` alone asked "is it absent?",
	// so `workspace_id = var.ws` with an unset variable arrived non-null and
	// blank: the provider default was skipped, the "Missing workspace"
	// diagnostic this helper exists to raise was skipped too, and the apply
	// posted to a path with an empty segment for the server to reject. Unknown
	// still counts as named -- it is a value that is not resolved yet.
	if !configured.IsNull() && (configured.IsUnknown() || configured.ValueString() != "") {
		return
	}
	var planned types.String
	if d := resp.Plan.GetAttribute(ctx, path.Root("workspace_id"), &planned); d.HasError() {
		return
	}
	// Carried over from state by UseStateForUnknown (whose attribute-level
	// modifier has run by now), so there is nothing to supply -- and supplying
	// one would move an existing resource to a different workspace.
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
