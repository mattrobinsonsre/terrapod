package workspace

import (
	"context"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/schema/validator"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

func audienceMap(t *testing.T, in map[string][]string) types.Map {
	t.Helper()
	elems := map[string]attr.Value{}
	for k, vs := range in {
		items := make([]attr.Value, 0, len(vs))
		for _, v := range vs {
			items = append(items, types.StringValue(v))
		}
		l, d := types.ListValue(types.StringType, items)
		if d.HasError() {
			t.Fatalf("building list for %q: %v", k, d)
		}
		elems[k] = l
	}
	m, d := types.MapValue(audienceElemType, elems)
	if d.HasError() {
		t.Fatalf("building map: %v", d)
	}
	return m
}

func runValidator(t *testing.T, m types.Map) *validator.MapResponse {
	t.Helper()
	resp := &validator.MapResponse{}
	audienceListsAreNonEmpty{}.ValidateMap(
		context.Background(),
		validator.MapRequest{Path: path.Root("oidc_audiences"), ConfigValue: m},
		resp,
	)
	return resp
}

// An empty list is neither "no audiences for this target" (said by removing the
// key) nor "take the catalogue" (said by an empty map). Accepting it would let
// an operator believe they had narrowed a target to nothing while the workspace
// is still minting for it.
func TestAnEmptyAudienceListIsRefusedAtPlanTime(t *testing.T) {
	resp := runValidator(t, audienceMap(t, map[string][]string{"aws": {}}))
	if !resp.Diagnostics.HasError() {
		t.Fatal("an empty list under a key was accepted")
	}
	// The key, not just "something is wrong" -- a workspace may name ten.
	var found bool
	for _, d := range resp.Diagnostics.Errors() {
		if strings.Contains(d.Detail(), `"aws"`) {
			found = true
		}
	}
	if !found {
		t.Fatalf("the diagnostic does not name the offending key: %v", resp.Diagnostics)
	}
}

// The remedy has to be in the message. "Remove the key" is not guessable from
// "empty list is invalid", and guessing wrong (setting `{}`) drops every
// override rather than one.
func TestTheRefusalNamesTheRemedy(t *testing.T) {
	resp := runValidator(t, audienceMap(t, map[string][]string{"vault.eu": {}}))
	joined := strings.ToLower(resp.Diagnostics.Errors()[0].Detail())
	for _, want := range []string{"remove the key", "oidc_audiences = {}"} {
		if !strings.Contains(joined, strings.ToLower(want)) {
			t.Errorf("the remedy %q is missing from: %s", want, joined)
		}
	}
}

func TestEveryOffendingKeyIsReported(t *testing.T) {
	resp := runValidator(t, audienceMap(t, map[string][]string{
		"aws": {}, "vault": {"tp"}, "azurerm": {},
	}))
	if n := len(resp.Diagnostics.Errors()); n != 2 {
		t.Fatalf("expected both empty keys reported, got %d: %v", n, resp.Diagnostics)
	}
}

// The empty MAP is the documented way to drop every override and fall back to
// the deployment catalogue. Refusing it here would remove the opt-out.
func TestAnEmptyMapIsAccepted(t *testing.T) {
	if resp := runValidator(t, audienceMap(t, map[string][]string{})); resp.Diagnostics.HasError() {
		t.Fatalf("an empty map was refused: %v", resp.Diagnostics)
	}
}

// Null is "this configuration does not manage the attribute"; unknown is what a
// create plans before apply. Neither is a practitioner naming an empty list,
// and failing on unknown would refuse every plan that computes the map.
func TestNullAndUnknownAreNotTheSameAsEmpty(t *testing.T) {
	for name, m := range map[string]types.Map{
		"null":    types.MapNull(audienceElemType),
		"unknown": types.MapUnknown(audienceElemType),
	} {
		if resp := runValidator(t, m); resp.Diagnostics.HasError() {
			t.Errorf("%s was refused: %v", name, resp.Diagnostics)
		}
	}
}

func TestAPopulatedListIsAccepted(t *testing.T) {
	m := audienceMap(t, map[string][]string{"aws": {"sts.amazonaws.com"}, "vault": {"a", "b"}})
	if resp := runValidator(t, m); resp.Diagnostics.HasError() {
		t.Fatalf("a valid map was refused: %v", resp.Diagnostics)
	}
}
