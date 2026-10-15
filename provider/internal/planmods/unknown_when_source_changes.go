package planmods

import (
	"context"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-go/tftypes"
)

// Derived names a Computed attribute whose value the SERVER derives from one or
// more configurable attributes.
type Derived struct {
	// Name is the derived Computed attribute.
	Name string
	// Sources are the attributes it is derived from. A change to any of them
	// changes Name as a side effect of the apply.
	Sources []string
}

// UnknownStringWhenSourceChanges is the mirror of KeepComputedWhenUnchanged,
// and the two exist for opposite halves of the same framework behaviour.
//
// `UseStateForUnknown` belongs only on a Computed attribute whose value does not
// change as a side effect of the apply. A name derived from an id is the case
// that looks like it qualifies and does not: it does not change on an ordinary
// PATCH, so it reads as stable, but it changes on precisely the applies that
// change its id -- and `UseStateForUnknown` then plans the OLD name while the
// apply's read-back returns the new one, which
// terraform-plugin-framework refuses as "Provider produced inconsistent result
// after apply" (#2110). The server-side change has already succeeded by then, so
// the operator sees a failed apply over a workspace that did move.
//
// `agent_pool_name` shipped carrying exactly that, and the resource's own
// "safe-for-UseStateForUnknown" comment listed it as safe WITH THE INVARIANT
// THAT IS THE BUG -- "only changes when agent_pool_id changes". So the guard is
// here, keyed on the source attribute, rather than left to each resource's
// ModifyPlan to remember: the class is demonstrably forgettable.
//
// Strings only, because every member of the class is a string today
// (`agent_pool_name`, `vcs_connection_name`). That is asserted by a test over
// the schema rather than assumed, so a future derived attribute of another type
// fails the gate instead of being silently skipped here.
func UnknownStringWhenSourceChanges(
	ctx context.Context,
	req resource.ModifyPlanRequest,
	resp *resource.ModifyPlanResponse,
	derived []Derived,
) {
	// Create has no prior value to have gone stale; destroy plans nothing.
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
	for _, d := range derived {
		for _, src := range d.Sources {
			p, okP := planned[src]
			s, okS := prior[src]
			if !okP || !okS {
				continue
			}
			// An unknown planned source counts as a change: whatever it
			// resolves to may differ, so the derived name cannot be promised.
			if p.Equal(s) {
				continue
			}
			resp.Diagnostics.Append(
				resp.Plan.SetAttribute(ctx, path.Root(d.Name), types.StringUnknown())...,
			)
			break
		}
	}
}
