package autodiscovery_rule

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// ruleResource builds a JSON:API resource carrying the given attributes.
//
// Defined here on the 1.7 line: on main the same helper comes with the
// engine-version tests (#1559), which this line predates.
func ruleResource(t *testing.T, attrs map[string]any) *terrapod.Resource {
	t.Helper()
	raw := map[string]json.RawMessage{}
	for k, v := range attrs {
		b, err := json.Marshal(v)
		if err != nil {
			t.Fatalf("marshal %s: %v", k, err)
		}
		raw[k] = b
	}
	return &terrapod.Resource{ID: "adr-1", Type: "autodiscovery-rules", Attributes: raw}
}

// #1748, reported against provider ~> 1.7.
//
// The configuration names its VCS connection and agent pool by interpolating a
// data source's `.id`, which is Terrapod's typed-prefix form ("vcs-…",
// "apool-…") -- the same form the provider's own schema descriptions use as
// their example. This endpoint serialises both attributes bare (a deliberate
// back-compat position: the prefixed ids live in `relationships`, #1063), so
// reading the server's answer verbatim replaced the configured value with one
// that names the same object but does not match it as a string. Terraform
// compares them as strings:
//
//	Error: Provider produced inconsistent result after apply
//	.agent_pool_id: was cty.StringVal("apool-XXXX"), but now cty.StringVal("XXXX")
//
// on every apply. These pin the round trip in both directions, because the
// bare form is the workaround people are running today and fixing one form at
// the cost of the other would be no fix at all.

const (
	poolUUID = "01a085db-3a9c-7fab-83a5-87b2d5e619d0"
	connUUID = "01a0a4aa-9d02-719d-b0be-ce434341268c"
)

// serverAnswer is what this endpoint really returns -- verified against a live
// stack, not assumed: bare uuids for both ids.
func serverAnswer(t *testing.T) map[string]any {
	t.Helper()
	return map[string]any{
		"name":              "rule",
		"vcs-connection-id": connUUID,
		"agent-pool-id":     poolUUID,
		"repo-url":          "https://github.com/example/repo",
		"pattern":           "*",
	}
}

func TestPrefixedIDsSurviveTheRoundTrip(t *testing.T) {
	// The plan, as the reporter wrote it.
	m := &autodiscoveryRuleModel{
		VCSConnectionID: types.StringValue("vcs-" + connUUID),
		AgentPoolID:     types.StringValue("apool-" + poolUUID),
	}

	if d := readAutodiscoveryRuleIntoModel(context.Background(), ruleResource(t, serverAnswer(t)), m); d.HasError() {
		t.Fatalf("read: %v", d)
	}

	if got := m.VCSConnectionID.ValueString(); got != "vcs-"+connUUID {
		t.Errorf("vcs_connection_id: got %q, want the configured %q", got, "vcs-"+connUUID)
	}
	if got := m.AgentPoolID.ValueString(); got != "apool-"+poolUUID {
		t.Errorf("agent_pool_id: got %q, want the configured %q", got, "apool-"+poolUUID)
	}
}

func TestBareIDsStillSurviveTheRoundTrip(t *testing.T) {
	// The documented workaround -- trimprefix(...) -- must keep working, or the
	// fix simply moves the error onto everyone who already applied it.
	m := &autodiscoveryRuleModel{
		VCSConnectionID: types.StringValue(connUUID),
		AgentPoolID:     types.StringValue(poolUUID),
	}

	if d := readAutodiscoveryRuleIntoModel(context.Background(), ruleResource(t, serverAnswer(t)), m); d.HasError() {
		t.Fatalf("read: %v", d)
	}

	if got := m.VCSConnectionID.ValueString(); got != connUUID {
		t.Errorf("vcs_connection_id: got %q, want the configured %q", got, connUUID)
	}
	if got := m.AgentPoolID.ValueString(); got != poolUUID {
		t.Errorf("agent_pool_id: got %q, want the configured %q", got, poolUUID)
	}
}

func TestAnIDChangedOutsideTerraformIsStillReported(t *testing.T) {
	// Keeping the configured form must not extend to hiding a real change: a
	// rule repointed at another pool outside Terraform has to show as drift.
	const otherPool = "01a0b524-c507-7d83-a87c-796de60b400d"

	m := &autodiscoveryRuleModel{
		VCSConnectionID: types.StringValue("vcs-" + connUUID),
		AgentPoolID:     types.StringValue("apool-" + poolUUID),
	}
	attrs := serverAnswer(t)
	attrs["agent-pool-id"] = otherPool

	if d := readAutodiscoveryRuleIntoModel(context.Background(), ruleResource(t, attrs), m); d.HasError() {
		t.Fatalf("read: %v", d)
	}

	if got := m.AgentPoolID.ValueString(); got != otherPool {
		t.Errorf("drift swallowed: got %q, want the server's %q", got, otherPool)
	}
}

func TestAnAbsentAgentPoolIsUnchanged(t *testing.T) {
	// agent_pool_id is Optional+Computed and the server sends null when a rule
	// has no pool. That has always read back as the empty string; keeping the
	// configured form must not quietly turn it into null.
	m := &autodiscoveryRuleModel{VCSConnectionID: types.StringValue("vcs-" + connUUID)}
	attrs := serverAnswer(t)
	attrs["agent-pool-id"] = nil

	if d := readAutodiscoveryRuleIntoModel(context.Background(), ruleResource(t, attrs), m); d.HasError() {
		t.Fatalf("read: %v", d)
	}

	if m.AgentPoolID.IsNull() || m.AgentPoolID.ValueString() != "" {
		t.Errorf("absent pool: got %#v, want the empty string", m.AgentPoolID)
	}
}
