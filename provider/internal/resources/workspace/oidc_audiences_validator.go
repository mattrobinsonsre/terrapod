package workspace

import (
	"context"
	"fmt"
	"sort"

	"github.com/hashicorp/terraform-plugin-framework/schema/validator"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// audienceListsAreNonEmpty refuses `{"aws": []}` at PLAN time (#1901).
//
// The server refuses it too, with a 422 naming the key, and that check is the
// authority — this is a public API and a provider is not the only client. But
// the server's refusal arrives during APPLY, which for a practitioner is the
// wrong half of the cycle: the plan says the workspace will be updated, the
// apply then fails, and on a workspace that is one resource among many the
// failure lands after other resources have already changed.
//
// So this is not duplicated business logic. "A list under a key must have at
// least one element" is a shape constraint, the kind a schema is for, and
// stating it here turns an apply-time failure into a plan-time one that names
// the key. The meaning of an empty list is what makes refusing it right: it is
// neither "no audiences for this target" (said by REMOVING the key, which falls
// back to the deployment catalogue) nor "take the catalogue" (said by an empty
// MAP). Accepting it would make an operator believe they had narrowed a target
// to nothing when the workspace is still minting for it.
//
// Written against the framework's own interface rather than pulled from
// terraform-plugin-framework-validators, which this module does not depend on:
// twenty lines against a new third-party module in the provider's supply chain
// is not a close call.
type audienceListsAreNonEmpty struct{}

var _ validator.Map = audienceListsAreNonEmpty{}

func (v audienceListsAreNonEmpty) Description(_ context.Context) string {
	return "every provider configuration must name at least one audience; remove the key instead"
}

func (v audienceListsAreNonEmpty) MarkdownDescription(ctx context.Context) string {
	return v.Description(ctx)
}

func (v audienceListsAreNonEmpty) ValidateMap(ctx context.Context, req validator.MapRequest, resp *validator.MapResponse) {
	if req.ConfigValue.IsNull() || req.ConfigValue.IsUnknown() {
		return
	}
	// Sorted, so a configuration with several offending keys reports them in a
	// stable order rather than Go's map order -- two runs of the same plan
	// should not produce differently ordered diagnostics.
	empties := make([]string, 0)
	for k, raw := range req.ConfigValue.Elements() {
		l, ok := raw.(types.List)
		if !ok || l.IsUnknown() {
			continue
		}
		// A null list and an explicitly empty one are the same mistake from the
		// practitioner's side: a key present with nothing under it.
		if l.IsNull() || len(l.Elements()) == 0 {
			empties = append(empties, k)
		}
	}
	if len(empties) == 0 {
		return
	}
	sort.Strings(empties)
	for _, k := range empties {
		resp.Diagnostics.AddAttributeError(
			req.Path.AtMapKey(k),
			"Provider configuration names no audiences",
			fmt.Sprintf(
				"oidc_audiences[%q] is an empty list, which has no meaning.\n\n"+
					"To stop overriding this provider configuration and fall back to the "+
					"deployment's audience catalogue, REMOVE the key. To drop every override, "+
					"set oidc_audiences = {}. To mint for this target, name at least one audience.",
				k,
			),
		)
	}
}
