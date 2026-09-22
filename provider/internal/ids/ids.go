// Package ids reconciles Terrapod's typed-prefix ids with the form a
// configuration happens to have written.
//
// Terrapod's house style is a typed-prefixed id -- "apool-…", "vcs-…", "ws-…"
// -- and that is what a data source's `.id` hands back, so it is the natural
// thing to interpolate into another resource. The write path accepts it either
// way, prefixed or bare, because the server strips the prefix before parsing
// the uuid.
//
// The read path is where that generosity has a cost. An endpoint may serialise
// the id back bare, and a resource that stores the server's answer verbatim has
// then replaced what the configuration said with something that differs from it
// textually while naming the same object. Terraform compares those as strings
// and fails the apply:
//
//	Error: Provider produced inconsistent result after apply
//	.agent_pool_id: was cty.StringVal("apool-XXXX"), but now cty.StringVal("XXXX")
//
// That was #1748, reported against terrapod_autodiscovery_rule. The remedy is
// not to pick a canonical form and rewrite the practitioner's config into it --
// that trades the error for a permanent diff on everyone who wrote the other
// form -- but to keep what they wrote whenever it names what the server
// returned. Two resources had already worked this out and each grew its own
// copy; this is that logic in one place, so the next id attribute inherits it
// rather than rediscovering it.
package ids

import (
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/types"
)

// Same reports whether two ids name the same object once the typed prefix is
// discounted, so "apool-X" and "X" are the same agent pool.
//
// The prefix is named by the caller rather than inferred. Inferring it would
// mean guessing where an id ends and its prefix begins, and a uuid contains
// hyphens of its own.
func Same(a, b, prefix string) bool {
	return strings.TrimPrefix(a, prefix) == strings.TrimPrefix(b, prefix)
}

// Keep returns what belongs in state for an id attribute: the form the
// configuration wrote, whenever that names the object the server returned, and
// the server's own value otherwise.
//
// `configured` is the value already in the model -- the plan on Create and
// Update, the prior state on Read -- so on a genuine change from outside
// Terraform the server still wins and the drift is reported.
func Keep(configured types.String, server, prefix string) types.String {
	if !configured.IsNull() && !configured.IsUnknown() &&
		Same(configured.ValueString(), server, prefix) {
		return configured
	}
	return types.StringValue(server)
}
