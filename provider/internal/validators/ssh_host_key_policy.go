// Package validators holds plan-time validators shared between resources, the
// same way internal/planmods holds shared plan modifiers.
//
// A validator lands here rather than in a resource package when more than one
// resource carries the attribute it guards. The SSH host-key policy is the first
// such case: `terrapod_workspace` holds the setting and
// `terrapod_autodiscovery_rule` templates it onto the workspaces it materialises,
// so both offer the same three values. Written once because the alternative is
// two copies of a value set, which is how two surfaces come to disagree about
// what they accept -- the same reason the server keeps one producer for it.
package validators

import (
	"context"
	"fmt"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/schema/validator"
)

// SSHHostKeyPolicies is the set the server accepts, strictest first.
//
// It is OpenSSH's `StrictHostKeyChecking` rather than Terrapod's own invention,
// which is what makes checking it here safe instead of a hostage to the server
// moving: OpenSSH has three behaviours and three is all there are. If a fourth
// ever appears, widening this is additive.
var SSHHostKeyPolicies = []string{"strict", "accept-new", "off"}

// SSHHostKeyPolicy refuses a policy the server would refuse, at PLAN time.
//
// The server's 422 is the authority -- this is a public API and a provider is
// not its only client -- so this is not the check, it is the same check moved to
// the half of the cycle a practitioner can act on. The server's refusal arrives
// during APPLY: the plan says the workspace will be updated, the apply then
// fails, and on a workspace that is one resource among many that failure lands
// after other resources have already changed.
//
// Validating client-side is right here and deliberately NOT right for every
// server-checked value. The test is whether the set can move underneath us. It
// cannot: these are OpenSSH's three behaviours, not a Terrapod list that could
// grow -- unlike, say, a host key's `key_type`, which is left to the server in
// `terrapod_inventory_host_keys` for exactly that reason. A validator over a set
// that CAN grow starts refusing values the server has begun accepting, which is
// worse than no validator at all.
//
// It also closes a hole an absent validator would leave, the same one
// `markerValidator` closes: `""` would be accepted here, refused by the server,
// and the practitioner would have no idea the empty string was not a way of
// saying "default".
type SSHHostKeyPolicy struct{}

var _ validator.String = SSHHostKeyPolicy{}

func (SSHHostKeyPolicy) Description(_ context.Context) string {
	return fmt.Sprintf("must be one of %s", strings.Join(SSHHostKeyPolicies, ", "))
}

func (v SSHHostKeyPolicy) MarkdownDescription(ctx context.Context) string {
	return v.Description(ctx)
}

func (SSHHostKeyPolicy) ValidateString(_ context.Context, req validator.StringRequest, resp *validator.StringResponse) {
	if req.ConfigValue.IsNull() || req.ConfigValue.IsUnknown() {
		return
	}
	if msg := SSHHostKeyPolicyProblem(req.ConfigValue.ValueString()); msg != "" {
		resp.Diagnostics.AddAttributeError(req.Path, "Invalid SSH host-key policy", msg)
	}
}

// SSHHostKeyPolicyProblem returns why value is not a policy, or "".
//
// Exported beside the validator so a test can drive the message without
// building a framework request, which is how markerProblem is tested.
func SSHHostKeyPolicyProblem(value string) string {
	if value == "" {
		return "a host-key policy must not be empty. Omit the attribute entirely to take the " +
			"server's default of \"strict\" — the server refuses \"\", so setting it would fail " +
			"the apply rather than mean anything."
	}
	for _, known := range SSHHostKeyPolicies {
		if value == known {
			return ""
		}
	}
	// A boolean is the likeliest mistake by some distance, because this
	// attribute REPLACED one -- `ansible_host_key_checking` (#2318) -- so a
	// configuration carried forward from it says `= true`. Terraform coerces
	// that to the string "true" before the provider sees it, so without this
	// clause the diagnostic would list three strings and leave the practitioner
	// to guess which of them their boolean had meant.
	switch strings.ToLower(value) {
	case "true":
		return "true is not a host-key policy. This attribute replaced " +
			"`ansible_host_key_checking`, which was a boolean, where `true` meant \"strict\". " +
			"Consider \"accept-new\" instead: it verifies a host whose key has been declared " +
			"and still reaches one nobody has pinned yet."
	case "false":
		return "false is not a host-key policy. This attribute replaced " +
			"`ansible_host_key_checking`, which was a boolean, where `false` meant \"off\" — " +
			"verifying nothing, for ever. Prefer \"accept-new\": it refuses a host whose key " +
			"has CHANGED while still reaching one on first contact."
	}
	return fmt.Sprintf(
		"%q is not a host-key policy. Expected %s — OpenSSH's StrictHostKeyChecking "+
			"behaviours, spelled as Terrapod spells them.",
		value, strings.Join(SSHHostKeyPolicies, ", "))
}
