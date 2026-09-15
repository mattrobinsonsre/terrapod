package planmods

import (
	"context"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-go/tftypes"
)

// KeepComputedWhenUnchanged is a resource-level ModifyPlan step: when every
// attribute in `compare` plans the same as the current state, the string
// attributes in `carry` keep their state values instead of going unknown.
//
// Why it is needed: the framework marks computed attributes unknown whenever
// the plan differs from state, and it decides that *after* applying defaults
// but *before* attribute plan modifiers run. `sensitive` defaults to false and
// is only forced back to true for a vault or git-auth variable by
// SensitiveForSecretBearingVariable, which runs later. So on a no-change
// re-plan of such a variable (with `sensitive` left out of the config, the
// documented form) the default briefly made the plan differ, `version_id` and
// `updated_at` went unknown, and every plan showed an in-place update that
// never converged (found under #1619).
//
// On a real change these attributes are left unknown, which is correct: the
// server does move them on every write.
func KeepComputedWhenUnchanged(
	ctx context.Context,
	req resource.ModifyPlanRequest,
	resp *resource.ModifyPlanResponse,
	compare, carry []string,
) {
	// Create and destroy have no prior state to keep.
	if req.State.Raw.IsNull() || req.Plan.Raw.IsNull() {
		return
	}
	var planned, prior map[string]tftypes.Value
	if err := resp.Plan.Raw.As(&planned); err != nil {
		return
	}
	if err := req.State.Raw.As(&prior); err != nil {
		return
	}
	for _, name := range compare {
		// An unknown planned value is never Equal to a known one, so anything
		// still being computed upstream counts as a change.
		if !planned[name].Equal(prior[name]) {
			return
		}
	}
	for _, name := range carry {
		var v types.String
		resp.Diagnostics.Append(req.State.GetAttribute(ctx, path.Root(name), &v)...)
		if resp.Diagnostics.HasError() {
			return
		}
		resp.Diagnostics.Append(resp.Plan.SetAttribute(ctx, path.Root(name), v)...)
	}
}
