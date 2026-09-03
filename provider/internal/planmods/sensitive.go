// Package planmods holds plan modifiers shared by more than one resource.
package planmods

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// gitAuthCategories mirrors GIT_AUTH_CATEGORIES in
// services/terrapod/services/variable_service.py.
var gitAuthCategories = map[string]bool{
	"git_http_auth": true,
	"git_ssh_auth":  true,
}

// sensitiveForSecretBearingVariable forces `sensitive` to true in the PLAN for
// the variables the server forces it on anyway.
//
// The server marks a variable sensitive regardless of what was asked whenever
// its value cannot be anything but a secret: a vault-sourced variable (whose
// reference resolves to one at run time) and the git-auth categories. Without
// this modifier, config that omits `sensitive` plans as a known false — the
// attribute is Optional+Computed with a false default — and then applies as
// true, which is the "Provider produced inconsistent result after apply" that
// terraform-plugin-framework raises, followed by a sensitive: true -> false
// diff that never converges because the default re-applies on every plan.
//
// Deriving the planned value here rather than dropping the default keeps plans
// readable for the ordinary case: an ordinary variable still plans a concrete
// false instead of "known after apply".
type sensitiveForSecretBearingVariable struct{}

// SensitiveForSecretBearingVariable returns the modifier described above. Put it
// on a `sensitive` attribute that sits alongside `value_source` and `category`.
func SensitiveForSecretBearingVariable() planmodifier.Bool {
	return sensitiveForSecretBearingVariable{}
}

func (m sensitiveForSecretBearingVariable) Description(context.Context) string {
	return "Forces sensitive to true for vault-sourced and git-auth variables, which the server marks sensitive regardless."
}

func (m sensitiveForSecretBearingVariable) MarkdownDescription(ctx context.Context) string {
	return m.Description(ctx)
}

func (m sensitiveForSecretBearingVariable) PlanModifyBool(
	ctx context.Context,
	req planmodifier.BoolRequest,
	resp *planmodifier.BoolResponse,
) {
	// Destroy plan — there is nothing to reconcile.
	if req.Plan.Raw.IsNull() {
		return
	}

	var source, category types.String
	resp.Diagnostics.Append(req.Plan.GetAttribute(ctx, path.Root("value_source"), &source)...)
	resp.Diagnostics.Append(req.Plan.GetAttribute(ctx, path.Root("category"), &category)...)
	if resp.Diagnostics.HasError() {
		return
	}

	// Either input still computed upstream: leave the value unknown rather than
	// guess, so apply can settle it either way without a mismatch.
	if source.IsUnknown() || category.IsUnknown() {
		resp.PlanValue = types.BoolUnknown()
		return
	}

	reason := ""
	switch {
	case source.ValueString() == "vault":
		reason = `value_source is "vault", so the value resolves to a secret at run time`
	case gitAuthCategories[category.ValueString()]:
		reason = fmt.Sprintf("category %q always holds a credential", category.ValueString())
	default:
		return
	}

	// Say so when the config explicitly asked for false, rather than silently
	// overriding it. Only fires on config that is actually wrong, so it does not
	// add noise to ordinary plans.
	if !req.ConfigValue.IsNull() && !req.ConfigValue.IsUnknown() && !req.ConfigValue.ValueBool() {
		resp.Diagnostics.AddAttributeWarning(
			req.Path,
			"sensitive is forced to true",
			"This variable is stored sensitive because "+reason+
				". The configured sensitive = false cannot take effect; remove it to silence this warning.",
		)
	}

	resp.PlanValue = types.BoolValue(true)
}
