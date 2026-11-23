package autodiscovery_rule

import (
	"context"
	"fmt"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/mattrobinsonsre/terrapod/provider/internal/validators"
)

// `ssh_host_key_policy` (#2592) on the autodiscovery rule, templated onto every
// workspace the rule materialises.
//
// This half is a PRE-EXISTING gap being closed rather than part of the rename.
// The server has carried this setting on the rule since #2318, under the
// boolean's name, and the provider never offered it -- so a Terraform-declared
// rule could not say anything about host-key verification, and the workspaces it
// created took the column default whatever the operator intended.
//
// `test_autodiscovery_rule_parity.py` ledgered that gap, and its stated reason
// was that "terrapod_workspace does not carry it either, so porting it here
// first would let a rule set something no workspace resource can then manage."
// That premise was false when it was written: `terrapod_workspace` has carried
// the setting since #2318. The ledger's gate checks only that a ledgered
// attribute really is absent from the surface, never that the REASON holds,
// which is how a wrong reason survives review -- so the entry goes with this.

func TestRuleHostKeyPolicyIsOnTheSchema(t *testing.T) {
	var resp resource.SchemaResponse
	NewResource().Schema(context.Background(), resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema build error: %v", resp.Diagnostics)
	}

	const name = "ssh_host_key_policy"
	a, ok := resp.Schema.Attributes[name]
	if !ok {
		t.Fatalf("%s is missing from the rule schema — a rule cannot template it onto "+
			"the workspaces it creates", name)
	}
	if !a.IsOptional() {
		t.Errorf("%s must be Optional", name)
	}
	// Optional+Computed throughout, the convention for every templated
	// workspace setting on this resource: the server answers with a concrete
	// value for every one of them, so Optional alone would plan null against it
	// and fail the apply of any rule that omits the attribute.
	if !a.IsComputed() {
		t.Errorf("%s must be Computed: the server supplies \"strict\" when the rule says "+
			"nothing, so an Optional-only attribute plans null against a concrete "+
			"answer and fails every apply that omits it", name)
	}
	sa, ok := a.(schema.StringAttribute)
	if !ok {
		t.Fatalf("%s is %T, want schema.StringAttribute", name, a)
	}

	// The validator must be the SHARED one, not a second copy. Two resources
	// each owning a value set is how they come to disagree about what they
	// accept, which is the reason internal/validators exists at all.
	want := fmt.Sprintf("%T", validators.SSHHostKeyPolicy{})
	for _, v := range sa.Validators {
		if fmt.Sprintf("%T", v) == want {
			return
		}
	}
	got := make([]string, 0, len(sa.Validators))
	for _, v := range sa.Validators {
		got = append(got, fmt.Sprintf("%T", v))
	}
	t.Errorf("%s has validators %v, want one of type %s — shared with "+
		"terrapod_workspace so the two surfaces cannot drift apart on the value set",
		name, got, want)
}

func TestRuleHostKeyPolicyReachesTheWire(t *testing.T) {
	for _, tc := range []struct {
		name    string
		val     types.String
		want    any // nil means the key must be absent from the wire
		present bool
	}{
		{"strict", types.StringValue("strict"), "strict", true},
		{"accept-new", types.StringValue("accept-new"), "accept-new", true},
		{"off", types.StringValue("off"), "off", true},
		{"omitted — the server's default stands", types.StringNull(), nil, false},
		{"unknown (create, no prior state)", types.StringUnknown(), nil, false},
		// The server refuses "", so sending it would fail the apply rather than
		// mean "take the default". Guarded like auto-apply-mode beside it.
		{"explicitly empty is not sent", types.StringValue(""), nil, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			attrs := buildAutodiscoveryRuleAttrs(&autodiscoveryRuleModel{SSHHostKeyPolicy: tc.val})
			got, ok := attrs["ssh-host-key-policy"]
			if ok != tc.present {
				t.Fatalf("ssh-host-key-policy present=%v (value %v), want present=%v",
					ok, got, tc.present)
			}
			if tc.present && got != tc.want {
				t.Errorf("ssh-host-key-policy = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestRuleHostKeyPolicyRoundTrips(t *testing.T) {
	ctx := context.Background()

	for _, want := range validators.SSHHostKeyPolicies {
		t.Run(want, func(t *testing.T) {
			var m autodiscoveryRuleModel
			res := ruleResource(t, map[string]any{"ssh-host-key-policy": want})
			if diags := readAutodiscoveryRuleIntoModel(ctx, res, &m); diags.HasError() {
				t.Fatalf("read: %v", diags)
			}
			if m.SSHHostKeyPolicy.IsNull() || m.SSHHostKeyPolicy.IsUnknown() {
				t.Fatalf("ssh_host_key_policy read back as %v, want a concrete value",
					m.SSHHostKeyPolicy)
			}
			if got := m.SSHHostKeyPolicy.ValueString(); got != want {
				t.Errorf("ssh_host_key_policy = %q, want %q", got, want)
			}
		})
	}
}

// TestRuleHostKeyPolicyFallsBackToStrict is the read-side case a round trip
// cannot reach: a rule read from a server that does not serialise the attribute
// at all. Projecting "" there would make an Optional+Computed attribute hold a
// value outside its own set, and would report the rule as templating NO policy
// when it templates the strictest one.
func TestRuleHostKeyPolicyFallsBackToStrict(t *testing.T) {
	ctx := context.Background()
	for _, tc := range []struct {
		name  string
		attrs map[string]any
	}{
		{"absent", map[string]any{"name": "r"}},
		{"explicitly empty", map[string]any{"name": "r", "ssh-host-key-policy": ""}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var m autodiscoveryRuleModel
			if diags := readAutodiscoveryRuleIntoModel(ctx, ruleResource(t, tc.attrs), &m); diags.HasError() {
				t.Fatalf("read: %v", diags)
			}
			if got := m.SSHHostKeyPolicy.ValueString(); got != "strict" {
				t.Errorf("ssh_host_key_policy = %q, want the strictest fallback \"strict\"", got)
			}
		})
	}
}
