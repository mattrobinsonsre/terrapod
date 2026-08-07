package provider

import (
	"context"
	"testing"

	fwresource "github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"

	adr "github.com/mattrobinsonsre/terrapod/provider/internal/resources/autodiscovery_rule"
	ws "github.com/mattrobinsonsre/terrapod/provider/internal/resources/workspace"
)

// TestAutoApplyHasNoSchemaDefault guards the #1294 fix.
//
// The server derives `auto_apply` from `auto_apply_mode` (`mode != "never"`),
// and the request builder sends ONLY the mode when one is configured. A schema
// default therefore makes the planned value a *known* false while the applied
// value comes back true, which Terraform core rejects outright:
//
//	Provider produced inconsistent result after apply:
//	.auto_apply: was cty.False, but now cty.True
//
// That made every non-`never` mode un-appliable on both resources.
//
// The schema golden cannot catch a regression here: it records optional and
// computed flags, not defaults, and stayed byte-identical across this fix.
// Hence an explicit assertion on the field itself.
func TestAutoApplyHasNoSchemaDefault(t *testing.T) {
	cases := []struct {
		name string
		res  fwresource.Resource
	}{
		{"terrapod_workspace", ws.NewResource()},
		{"terrapod_autodiscovery_rule", adr.NewResource()},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			resp := &fwresource.SchemaResponse{}
			tc.res.Schema(context.Background(), fwresource.SchemaRequest{}, resp)

			attr, ok := resp.Schema.Attributes["auto_apply"]
			if !ok {
				t.Fatalf("%s has no auto_apply attribute", tc.name)
			}
			boolAttr, ok := attr.(schema.BoolAttribute)
			if !ok {
				t.Fatalf("%s.auto_apply is %T, expected schema.BoolAttribute", tc.name, attr)
			}

			if boolAttr.Default != nil {
				t.Errorf(
					"%s.auto_apply must NOT carry a schema default. The server projects "+
						"it from auto_apply_mode, so a known planned value produces "+
						"'inconsistent result after apply' and no conditional mode can be "+
						"applied at all (#1294).",
					tc.name,
				)
			}
			// Optional+Computed is what lets the server's projection land
			// without a planned value to contradict it — the default's removal
			// is only safe in that combination.
			if !boolAttr.Optional || !boolAttr.Computed {
				t.Errorf(
					"%s.auto_apply must stay Optional+Computed (got optional=%v computed=%v)",
					tc.name, boolAttr.Optional, boolAttr.Computed,
				)
			}
		})
	}
}

// TestAutoApplyModeIsSentIndependentlyOfTheBoolean guards the second half.
//
// On terrapod_autodiscovery_rule the mode branch was originally nested inside
// an `auto_apply is known` guard, which only worked while the boolean carried
// a default. Removing that default would have silently stopped `auto-apply-mode`
// being sent at all — the two defects masked each other, so fixing one without
// the other is worse than fixing neither.
func TestAutoApplyModeIsNotGatedOnTheBoolean(t *testing.T) {
	resp := &fwresource.SchemaResponse{}
	adr.NewResource().Schema(context.Background(), fwresource.SchemaRequest{}, resp)

	mode, ok := resp.Schema.Attributes["auto_apply_mode"]
	if !ok {
		t.Fatal("terrapod_autodiscovery_rule has no auto_apply_mode attribute")
	}
	strAttr, ok := mode.(schema.StringAttribute)
	if !ok {
		t.Fatalf("auto_apply_mode is %T, expected schema.StringAttribute", mode)
	}
	// If the mode were ever given a default it would always be "known" and the
	// either/or the API enforces (422 when both are sent) becomes reachable.
	if strAttr.Default != nil {
		t.Error("auto_apply_mode must not carry a default — the API 422s when both are sent")
	}
}
