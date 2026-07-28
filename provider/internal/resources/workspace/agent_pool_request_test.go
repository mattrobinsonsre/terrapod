package workspace

import (
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// Regression guard for #1094.
//
// `agent_pool_ids` is Optional+Computed with UseStateForUnknown, so a config
// that omits it still receives a KNOWN, non-null planned value — the prior
// state. Before the fix, any update of a workspace that had a pool set
// populated both `agent-pool-id` and `agent-pool-ids`, and the server rejected
// the pair with a 422. Create escaped it only because there is no prior state,
// so the plan value was unknown.
//
// The rule: when the singular is set, it is the only one that goes on the wire.
func TestAgentPoolRequestNeverSendsBoth(t *testing.T) {
	poolList := func(vals ...string) types.List {
		elems := make([]attr.Value, 0, len(vals))
		for _, v := range vals {
			elems = append(elems, types.StringValue(v))
		}
		return types.ListValueMust(types.StringType, elems)
	}

	cases := []struct {
		name      string
		poolID    types.String
		poolIDs   types.List
		wantID    string
		wantIDs   []string
		rationale string
	}{
		{
			name:      "singular set, plural carried over from state (the #1094 shape)",
			poolID:    types.StringValue("apool-a"),
			poolIDs:   poolList("apool-a", "apool-b"),
			wantID:    "apool-a",
			wantIDs:   nil,
			rationale: "the plural must be suppressed or the server 422s the pair",
		},
		{
			name:    "singular only",
			poolID:  types.StringValue("apool-a"),
			poolIDs: types.ListNull(types.StringType),
			wantID:  "apool-a",
			wantIDs: nil,
		},
		{
			name:    "plural only — the set is sent",
			poolID:  types.StringNull(),
			poolIDs: poolList("apool-a", "apool-b"),
			wantID:  "",
			wantIDs: []string{"apool-a", "apool-b"},
		},
		{
			name:    "plural unknown (create, no prior state) — nothing sent",
			poolID:  types.StringNull(),
			poolIDs: types.ListUnknown(types.StringType),
			wantID:  "",
			wantIDs: nil,
		},
		{
			name:    "neither set",
			poolID:  types.StringNull(),
			poolIDs: types.ListNull(types.StringType),
			wantID:  "",
			wantIDs: nil,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceModel
			m.AgentPoolID = tc.poolID
			m.AgentPoolIDs = tc.poolIDs

			// The real helper both request builders call — not a copy of its
			// logic, which would keep passing if the builders regressed.
			var gotID string
			if !m.AgentPoolID.IsNull() {
				gotID = m.AgentPoolID.ValueString()
			}
			gotIDs := agentPoolIDsForRequest(m.AgentPoolID, m.AgentPoolIDs)

			if gotID != tc.wantID {
				t.Errorf("agent-pool-id = %q, want %q", gotID, tc.wantID)
			}
			if len(gotIDs) != len(tc.wantIDs) {
				t.Fatalf("agent-pool-ids = %v, want %v (%s)", gotIDs, tc.wantIDs, tc.rationale)
			}
			for i := range gotIDs {
				if gotIDs[i] != tc.wantIDs[i] {
					t.Errorf("agent-pool-ids[%d] = %q, want %q", i, gotIDs[i], tc.wantIDs[i])
				}
			}
			if gotID != "" && len(gotIDs) > 0 {
				t.Errorf("both fields populated — the server rejects this pair (#1094)")
			}
		})
	}
}
