package autodiscovery_rule

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// The rule templates allow_fork_pr_plans onto the workspaces it creates, so an
// autodiscovered workspace inherits the posture the rule declares rather than
// silently falling back to the server default (GHSA-gp5w-76rw-c452). The
// omitted case matters as much as the set one: a rule that does not mention it
// must leave the server's value alone, not plan it as false.
func TestRuleAllowForkPRPlansAttrs(t *testing.T) {
	cases := []struct {
		name    string
		val     types.Bool
		want    any // nil means the key must be absent from the wire
		present bool
	}{
		{"enabled for a public module repo's workspaces", types.BoolValue(true), true, true},
		{"explicitly disabled", types.BoolValue(false), false, true},
		{"omitted — the server's default stands", types.BoolNull(), nil, false},
		{"unknown (create, no prior state)", types.BoolUnknown(), nil, false},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			m := &autodiscoveryRuleModel{AllowForkPRPlans: tc.val}
			attrs := buildAutodiscoveryRuleAttrs(m)

			got, ok := attrs["allow-fork-pr-plans"]
			if ok != tc.present {
				t.Fatalf("allow-fork-pr-plans present=%v (value %v), want present=%v", ok, got, tc.present)
			}
			if tc.present && got != tc.want {
				t.Errorf("allow-fork-pr-plans = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestRuleAllowForkPRPlansRoundTrips(t *testing.T) {
	ctx := context.Background()

	for _, want := range []bool{true, false} {
		var m autodiscoveryRuleModel
		res := ruleResource(t, map[string]any{"allow-fork-pr-plans": want})
		if diags := readAutodiscoveryRuleIntoModel(ctx, res, &m); diags.HasError() {
			t.Fatalf("read: %v", diags)
		}
		if m.AllowForkPRPlans.IsNull() || m.AllowForkPRPlans.IsUnknown() {
			t.Fatalf("allow_fork_pr_plans read back as %v, want a concrete value", m.AllowForkPRPlans)
		}
		if got := m.AllowForkPRPlans.ValueBool(); got != want {
			t.Errorf("allow_fork_pr_plans = %v, want %v", got, want)
		}
	}
}

func TestRuleAllowForkPRPlansIsOptionalAndComputed(t *testing.T) {
	var resp resource.SchemaResponse
	NewResource().Schema(context.Background(), resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema build error: %v", resp.Diagnostics)
	}

	attr, ok := resp.Schema.Attributes["allow_fork_pr_plans"]
	if !ok {
		t.Fatal("allow_fork_pr_plans attribute is missing")
	}
	if !attr.IsOptional() || !attr.IsComputed() {
		t.Errorf("allow_fork_pr_plans must be Optional+Computed (#684); optional=%v computed=%v",
			attr.IsOptional(), attr.IsComputed())
	}
}
