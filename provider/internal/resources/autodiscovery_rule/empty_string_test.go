package autodiscovery_rule

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// `ansible_execution_environment` is the one attribute on this resource that
// fixes the explicit-empty-string defect (#2310) WITHOUT moving to
// Optional+Computed+UseStateForUnknown, and the cases below are the whole
// reason it can be: the read-back consults the configuration instead.
//
// The defect, in the general case: an Optional+Computed attribute plans an
// explicit `""` as `""`, so a read-back that collapses the server's `""` to
// null fails the apply with "Provider produced inconsistent result after
// apply". Seven sibling attributes were fixed by making the read-back
// unconditional and letting Computed supply the server's value.
//
// This attribute cannot take that shape, and its schema records why: the
// server answers `""` when no label is templated and has no value to supply,
// so Computed would write `""` into state against a null config and plan a
// change on every run. `optionalStringKeepingExplicitEmpty` therefore keeps
// null only when the server is empty AND the configuration asked for nothing.
//
// That makes correctness a PAIRING rather than a single behaviour, so a test
// covering fewer than all four combinations proves nothing about the trade:
//
//	configured  | server | want | why
//	------------+--------+------+------------------------------------------
//	null        | ""     | null | an omitted attribute must not acquire ""
//	unknown     | ""     | null | same, during create/update
//	"" explicit | ""     | ""   | the #2310 defect: an explicit empty must
//	            |        |      | round-trip
//	"x"         | "x"    | "x"  | the ordinary case still works
//
// The two directions pull against each other: collapsing every `""` to null
// breaks row 3, and projecting every server value unconditionally -- the
// careless "fix" -- breaks rows 1 and 2, which is exactly what the recorded
// decision against Computed forbids.

func TestTheExecutionEnvironmentReadBackHonoursBothDirections(t *testing.T) {
	// Driven through the real read-back rather than the helper alone, because
	// the defect lived in the CALL SITE: the helper can be perfectly correct
	// while `readAutodiscoveryRuleIntoModel` goes on collapsing the value, and
	// a helper-only test cannot see that.
	ctx := context.Background()

	for _, tc := range []struct {
		name string
		// configured is the model's own value before the read -- the PLANNED
		// value during create/update and prior state during a refresh, which
		// is how the read-back receives it.
		configured types.String
		attrs      map[string]any
		wantNull   bool
		want       string
	}{
		{
			name:       "an omitted attribute stays null against an empty server answer",
			configured: types.StringNull(),
			attrs:      map[string]any{"name-template": "x", "ansible-execution-environment": ""},
			wantNull:   true,
		},
		{
			// The same case reached a different way: a server with nothing to
			// say omits the key entirely and GetStringAttr reports "". Both
			// forms must stay null.
			name:       "an absent key is the same as an empty one",
			configured: types.StringNull(),
			attrs:      map[string]any{"name-template": "x"},
			wantNull:   true,
		},
		{
			// Unknown is what the model holds for an unset Optional attribute
			// mid-apply, so it must behave exactly like null -- otherwise a
			// rule that never declared the attribute acquires "" on create.
			name:       "unknown is not a declaration either",
			configured: types.StringUnknown(),
			attrs:      map[string]any{"name-template": "x", "ansible-execution-environment": ""},
			wantNull:   true,
		},
		{
			// The #2310 defect itself. `ansible_execution_environment = ""` is
			// a real declaration and it plans as "", so a null read-back is
			// the inconsistent-result failure.
			name:       "an explicit empty string round-trips as an empty string",
			configured: types.StringValue(""),
			attrs:      map[string]any{"name-template": "x", "ansible-execution-environment": ""},
			want:       "",
		},
		{
			name:       "an ordinary value round-trips",
			configured: types.StringValue("hardened-ee"),
			attrs:      map[string]any{"name-template": "x", "ansible-execution-environment": "hardened-ee"},
			want:       "hardened-ee",
		},
		{
			// A value the configuration did not ask for is still reported,
			// because only an EMPTY server answer is ambiguous -- a concrete
			// one is the server's real state and belongs in state.
			name:       "a server value survives a null config",
			configured: types.StringNull(),
			attrs:      map[string]any{"name-template": "x", "ansible-execution-environment": "hardened-ee"},
			want:       "hardened-ee",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			m := &autodiscoveryRuleModel{AnsibleExecutionEnvironment: tc.configured}
			if d := readAutodiscoveryRuleIntoModel(ctx, ruleResource(t, tc.attrs), m); d.HasError() {
				t.Fatalf("read: %v", d)
			}
			got := m.AnsibleExecutionEnvironment

			if tc.wantNull {
				// Asserting null specifically, not merely "not the server's
				// value": an unknown left in state fails an apply too, so the
				// two are distinguished rather than both passing.
				if got.IsUnknown() {
					t.Fatalf("ansible_execution_environment read back UNKNOWN from a "+
						"%v config and attrs %v -- an unknown in state fails the apply "+
						"just as a wrong value does", tc.configured, tc.attrs)
				}
				if !got.IsNull() {
					t.Errorf("ansible_execution_environment = %q from a %v config and "+
						"an empty server answer, want null -- the schema records a "+
						"deliberate decision against Computed precisely so an omitted "+
						"attribute does not acquire \"\" and diff for ever",
						got.ValueString(), tc.configured)
				}
				return
			}

			if got.IsNull() || got.IsUnknown() {
				t.Fatalf("ansible_execution_environment read back as %v from a %v "+
					"config, want %q -- collapsing a declared value is \"Provider "+
					"produced inconsistent result after apply\"", got, tc.configured, tc.want)
			}
			if got.ValueString() != tc.want {
				t.Errorf("ansible_execution_environment = %q, want %q",
					got.ValueString(), tc.want)
			}
		})
	}
}

func TestTheExecutionEnvironmentStaysOptionalOnly(t *testing.T) {
	// This test exists to stop someone "tidying" the attribute onto the
	// Optional+Computed+UseStateForUnknown shape its seven siblings use, which
	// would silently reverse the decision recorded in the schema: the server
	// returns "" when no label is templated and has no value to supply, so
	// Computed would put "" into state against a null config and plan a change
	// on every run for every rule that never mentions the attribute.
	//
	// Adding Computed here also makes the read-back's configuration-consulting
	// shape pointless, so the two move together or not at all.
	var resp resource.SchemaResponse
	NewResource().Schema(context.Background(), resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema build error: %v", resp.Diagnostics)
	}

	a, ok := resp.Schema.Attributes["ansible_execution_environment"]
	if !ok {
		t.Fatal("ansible_execution_environment is missing from the rule schema")
	}
	if !a.IsOptional() {
		t.Error("ansible_execution_environment must be Optional -- a rule templates it " +
			"onto the workspaces it creates")
	}
	if a.IsComputed() {
		t.Error("ansible_execution_environment must NOT be Computed: the schema records " +
			"a deliberate decision against it, because the server answers \"\" when no " +
			"label is templated and Computed would write that \"\" into state against a " +
			"null config. The empty-string defect is fixed here by consulting the " +
			"configuration in the read-back instead -- see " +
			"optionalStringKeepingExplicitEmpty")
	}
	if a.IsRequired() {
		t.Error("ansible_execution_environment must not be Required -- leaving it unset " +
			"is the ordinary case")
	}
}
