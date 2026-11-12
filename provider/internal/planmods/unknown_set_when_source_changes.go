package planmods

import (
	"context"

	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-go/tftypes"
)

// UnknownSetWhenSourceChanges is `UseStateForUnknown` for an Optional+Computed
// SET whose value the server DERIVES from sibling configurable attributes --
// with the one escape hatch `UseStateForUnknown` cannot express: when the
// configuration moves one of those sources, the prior value is stale, so the
// plan must leave the set unknown for the server to re-derive.
//
// It differs from `UnknownStringWhenSourceChanges` in two ways, and both are
// what make it right for an attribute the practitioner MAY author:
//
//   - it does nothing when the attribute is present in configuration. An
//     authored value is the stored truth and the sources become its summary, so
//     there is no derivation to invalidate;
//   - it otherwise CARRIES THE PRIOR VALUE FORWARD, rather than only ever
//     forcing unknown. That is the half that keeps ordinary plans readable: an
//     edit to some unrelated attribute must not turn the set into "known after
//     apply" on every plan (the #1619 never-converging-plan-noise class).
//
// **A source is only consulted when the CONFIGURATION names it.** The sources
// here are themselves Optional+Computed, so an unauthored one holds the
// server's own derived summary — and a summary is not an instruction. Comparing
// the planned value instead would read every unauthored source as "changed"
// (its own `UseStateForUnknown` may not have run yet, so it is still unknown),
// which forces the set unknown on every plan; and reading it as authored input
// is the very defect this modifier exists to fix, one field over.
//
// The defect (#2502): `terrapod_role.capabilities` is Optional+Computed and
// carried `UseStateForUnknown`, so a role that does not author it planned the
// PRIOR DERIVED capability set rather than unknown. The update then sent that
// set, the server treats any `capabilities` in a request as authored and stores
// it verbatim, and the level edit the practitioner actually asked for was
// dropped — leaving `workspace_permission: "custom" -> "write"` planned against
// a read-back of "custom", which the framework refuses as "Provider produced
// inconsistent result after apply". Every later apply of the configuration was
// blocked behind it.
type unknownSetWhenSourceChanges struct{ sources []string }

// UnknownSetWhenSourceChanges returns the modifier described above, for a set
// derived from `sources`. Put it on an Optional+Computed set attribute INSTEAD
// of `setplanmodifier.UseStateForUnknown()`, never alongside it: that one would
// settle the unknown back to the stale value and undo this.
func UnknownSetWhenSourceChanges(sources ...string) planmodifier.Set {
	return unknownSetWhenSourceChanges{sources: sources}
}

func (m unknownSetWhenSourceChanges) Description(context.Context) string {
	return "Keeps the prior value unless the configuration changes an attribute the server derives it from, when it is left unknown to be re-derived."
}

func (m unknownSetWhenSourceChanges) MarkdownDescription(ctx context.Context) string {
	return m.Description(ctx)
}

// Sources reports the attributes this modifier watches. It exists so a resource
// can GATE that every one of its derived sources is wired, rather than a test
// comparing descriptions -- which are a constant and say nothing about the
// sources, so such a gate would pass with sources missing. A level that is not
// a source is a level whose edit goes back to being silently dropped.
func (m unknownSetWhenSourceChanges) Sources() []string {
	out := make([]string, len(m.sources))
	copy(out, m.sources)
	return out
}

func (m unknownSetWhenSourceChanges) PlanModifySet(
	ctx context.Context,
	req planmodifier.SetRequest,
	resp *planmodifier.SetResponse,
) {
	// Authored: the planned value is the configured one and the sources are its
	// summary. Nothing here to decide.
	if !req.ConfigValue.IsNull() {
		return
	}
	// Create has no prior value to carry forward; destroy plans nothing.
	if req.State.Raw.IsNull() || req.Plan.Raw.IsNull() {
		return
	}

	var cfg, prior map[string]tftypes.Value
	if err := req.Config.Raw.As(&cfg); err != nil {
		return
	}
	if err := req.State.Raw.As(&prior); err != nil {
		return
	}

	for _, src := range m.sources {
		c, okC := cfg[src]
		s, okS := prior[src]
		if !okC || !okS {
			continue
		}
		// Unauthored: a derived summary, not something the practitioner asked
		// to change, so it cannot be what makes the set stale.
		if c.IsNull() {
			continue
		}
		// An unknown configured source counts as a change: whatever it
		// resolves to may differ, so the prior set cannot be promised.
		if c.IsKnown() && c.Equal(s) {
			continue
		}
		// Explicit rather than "return and let the framework's unknown
		// stand", so the behaviour is the same however the planned value
		// arrived here.
		resp.PlanValue = types.SetUnknown(req.StateValue.ElementType(ctx))
		return
	}

	resp.PlanValue = req.StateValue
}
