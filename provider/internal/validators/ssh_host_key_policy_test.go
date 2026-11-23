package validators

import (
	"context"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/schema/validator"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

func TestSSHHostKeyPolicyProblem(t *testing.T) {
	for _, tc := range []struct {
		value string
		// A substring the message must carry. Empty means the value is valid
		// and the function must say nothing at all.
		wants string
	}{
		{"strict", ""},
		{"accept-new", ""},
		{"off", ""},
		// Empty is its own case, because it is the one a practitioner reaches
		// for meaning "default" and the server refuses outright.
		{"", "Omit the attribute entirely"},
		// The two the rename makes likely. These exist so the diagnostic names
		// the retired attribute rather than listing three strings at somebody
		// whose configuration says `= true`.
		{"true", "ansible_host_key_checking"},
		{"false", "ansible_host_key_checking"},
		// Case-insensitively, because HCL's `true` and a quoted "True" reach
		// the provider as different strings and mean the same mistake.
		{"TRUE", "ansible_host_key_checking"},
		// Anything else falls through to the generic form, which must name the
		// three values so the message is actionable on its own.
		{"paranoid", "strict, accept-new, off"},
		// A near miss on the middle value, which is the typo most likely to be
		// made: OpenSSH's own spelling is hyphenated.
		{"accept_new", "strict, accept-new, off"},
	} {
		t.Run(tc.value, func(t *testing.T) {
			got := SSHHostKeyPolicyProblem(tc.value)
			if tc.wants == "" {
				if got != "" {
					t.Errorf("%q is a policy, but was refused: %s", tc.value, got)
				}
				return
			}
			if got == "" {
				t.Fatalf("%q was accepted, but it is not a policy", tc.value)
			}
			if !strings.Contains(got, tc.wants) {
				t.Errorf("%q: message %q does not carry %q", tc.value, got, tc.wants)
			}
		})
	}
}

// TestTheValidatorReportsThroughTheFramework drives the validator itself rather
// than the helper beneath it: the helper could be perfect while the validator
// swallowed its result, which is the shape a message-only test cannot see.
func TestTheValidatorReportsThroughTheFramework(t *testing.T) {
	for _, tc := range []struct {
		name      string
		value     types.String
		wantError bool
	}{
		{"a policy passes", types.StringValue("accept-new"), false},
		{"a non-policy is refused", types.StringValue("paranoid"), true},
		{"the empty string is refused", types.StringValue(""), true},
		// Null and unknown must pass: null is the configuration omitting the
		// attribute, which is legitimate and takes the server default, and
		// unknown is a value not yet resolved at plan time. Refusing either
		// would fail a plan for saying nothing.
		{"null passes — the attribute is optional", types.StringNull(), false},
		{"unknown passes — nothing to judge yet", types.StringUnknown(), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			resp := &validator.StringResponse{}
			SSHHostKeyPolicy{}.ValidateString(context.Background(), validator.StringRequest{
				Path:        path.Root("ssh_host_key_policy"),
				ConfigValue: tc.value,
			}, resp)
			if got := resp.Diagnostics.HasError(); got != tc.wantError {
				t.Errorf("HasError() = %v, want %v (diags: %v)",
					got, tc.wantError, resp.Diagnostics)
			}
		})
	}
}

// TestTheSetIsOpenSSHsThree pins the set itself, which is what makes validating
// client-side defensible. The server holds the same three in
// SSH_HOST_KEY_POLICIES; a fourth appearing on one side and not the other is
// the drift this exists to make loud.
func TestTheSetIsOpenSSHsThree(t *testing.T) {
	want := []string{"strict", "accept-new", "off"}
	if len(SSHHostKeyPolicies) != len(want) {
		t.Fatalf("SSHHostKeyPolicies = %v, want %v", SSHHostKeyPolicies, want)
	}
	for i, v := range want {
		if SSHHostKeyPolicies[i] != v {
			t.Errorf("SSHHostKeyPolicies[%d] = %q, want %q — the order is strictest "+
				"first, which is how the diagnostic reads them out", i, SSHHostKeyPolicies[i], v)
		}
	}
}

func TestTheDescriptionNamesEveryValue(t *testing.T) {
	// The description is what Terraform prints for `terraform providers schema`
	// and in some diagnostics, so a description that named only some of the set
	// would be a worse answer than none.
	v := SSHHostKeyPolicy{}
	d := v.Description(context.Background())
	for _, policy := range SSHHostKeyPolicies {
		if !strings.Contains(d, policy) {
			t.Errorf("description %q does not name %q", d, policy)
		}
	}
	if v.MarkdownDescription(context.Background()) != d {
		t.Error("MarkdownDescription must agree with Description")
	}
}
