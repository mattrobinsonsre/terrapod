package workspace

import (
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// The #1091 warning turns on two predicates: the config value being null, and
// the state value holding at least one element. `hasElements` is the second, and
// it has to be right about every shape a list attribute can be in — a null or
// unknown list is not "a value the config isn't managing", and neither is an
// empty one (the server default), so none of them should raise a warning.
func TestHasElements(t *testing.T) {
	strList := func(vals ...string) types.List {
		elems := make([]attr.Value, 0, len(vals))
		for _, v := range vals {
			elems = append(elems, types.StringValue(v))
		}
		return types.ListValueMust(types.StringType, elems)
	}

	cases := []struct {
		name string
		val  attr.Value
		want bool
	}{
		{"null list is not a value to warn about", types.ListNull(types.StringType), false},
		{"unknown list is not a value to warn about", types.ListUnknown(types.StringType), false},
		{"empty list is the server default, not an unmanaged value", strList(), false},
		{"one element is an unmanaged value", strList("envs/prod.tfvars"), true},
		{"several elements likewise", strList("a.tfvars", "b.tfvars"), true},
		{"a non-list value never warns", types.StringValue("nope"), false},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := hasElements(tc.val); got != tc.want {
				t.Errorf("hasElements(%v) = %v, want %v", tc.val, got, tc.want)
			}
		})
	}
}

// Every entry in unmanagedCollections must actually read a distinct field off
// the model. A copy-paste that left two entries pointing at the same field would
// silence the warning for one attribute and double it for another, and nothing
// else would catch it.
func TestUnmanagedCollectionAccessorsAreDistinct(t *testing.T) {
	strList := func(s string) types.List {
		return types.ListValueMust(types.StringType, []attr.Value{types.StringValue(s)})
	}

	for _, target := range unmanagedCollections {
		// Populate exactly one field, then assert only that entry's accessor sees it.
		var m workspaceModel
		m.AgentPoolIDs = types.ListNull(types.StringType)
		m.VarFiles = types.ListNull(types.StringType)
		m.TriggerPrefixes = types.ListNull(types.StringType)
		m.DriftIgnoreRules = types.ListNull(types.StringType)
		m.SecurityScanSkipRules = types.ListNull(types.StringType)

		switch target.name {
		case "agent_pool_ids":
			m.AgentPoolIDs = strList("apool-x")
		case "var_files":
			m.VarFiles = strList("envs/prod.tfvars")
		case "trigger_prefixes":
			m.TriggerPrefixes = strList("modules/shared")
		case "drift_ignore_rules":
			m.DriftIgnoreRules = strList("aws_iam_role.foo")
		case "security_scan_skip_rules":
			m.SecurityScanSkipRules = strList("CKV_AWS_24")
		default:
			t.Fatalf("unmanagedCollections gained %q with no case here — extend this test", target.name)
		}

		for _, a := range unmanagedCollections {
			got := hasElements(a.value(&m))
			want := a.name == target.name
			if got != want {
				t.Errorf("with only %s populated, accessor for %s returned %v (want %v) — "+
					"the accessors are crossed", target.name, a.name, got, want)
			}
		}
	}
}
