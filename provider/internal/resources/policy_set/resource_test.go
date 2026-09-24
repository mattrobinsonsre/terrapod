package policy_set

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// #1765. A policy set can block applies across the whole estate, and it was
// the one gate manageable only through the admin UI. These cover the two ways
// a resource like this goes wrong in practice: a scope that cannot be narrowed
// by deleting a line, and an optional field that plans forever because the
// server answers an unset string with "".

func listOf(t *testing.T, vals ...string) types.List {
	t.Helper()
	elems := make([]attr.Value, 0, len(vals))
	for _, v := range vals {
		elems = append(elems, types.StringValue(v))
	}
	l, d := types.ListValue(types.StringType, elems)
	if d.HasError() {
		t.Fatalf("building list: %v", d)
	}
	return l
}

func mapOf(t *testing.T, entries map[string]types.List) types.Map {
	t.Helper()
	vals := make(map[string]attr.Value, len(entries))
	for k, v := range entries {
		vals[k] = v
	}
	m, d := types.MapValue(types.ListType{ElemType: types.StringType}, vals)
	if d.HasError() {
		t.Fatalf("building map: %v", d)
	}
	return m
}

func TestScopeIsSentAsAMapOfLists(t *testing.T) {
	// The server matches a policy set's scope with the same code that matches
	// a role's, so one key may bind several accepted values:
	// { env = ["prod", "stg"] } reads as "env is prod OR stg".
	m := &policySetModel{
		Name: types.StringValue("baseline"),
		AllowLabels: mapOf(t, map[string]types.List{
			"env": listOf(t, "prod", "stg"),
		}),
		AllowNames: listOf(t, "payments"),
	}

	req := buildCreateRequest(m)

	if got := req.AllowLabels["env"]; len(got) != 2 || got[0] != "prod" || got[1] != "stg" {
		t.Fatalf("allow_labels[env] = %v, want [prod stg]", got)
	}
	if len(req.AllowNames) != 1 || req.AllowNames[0] != "payments" {
		t.Fatalf("allow_names = %v", req.AllowNames)
	}
}

func TestAnUpdateCanNarrowTheScopeToNothing(t *testing.T) {
	// Deleting every allow label from the configuration must send an EMPTY
	// map, not omit the field. Omitting it would mean "leave it alone", and a
	// scope that cannot be narrowed by removing a line is not really managed
	// as code -- worse here than elsewhere, because widening a policy set's
	// scope silently is how a gate stops covering what it was meant to.
	m := &policySetModel{
		Name:        types.StringValue("baseline"),
		AllowLabels: types.MapNull(types.ListType{ElemType: types.StringType}),
		AllowNames:  types.ListNull(types.StringType),
	}

	req := buildUpdateRequest(m)

	if req.AllowLabels == nil {
		t.Fatal("allow_labels must be an empty map on update, not nil")
	}
	if len(req.AllowLabels) != 0 {
		t.Fatalf("allow_labels = %v, want empty", req.AllowLabels)
	}
	if req.AllowNames == nil || len(req.AllowNames) != 0 {
		t.Fatalf("allow_names = %v, want empty non-nil", req.AllowNames)
	}
}

func TestACreateOmitsAnUnsetScopeRatherThanEmptyingIt(t *testing.T) {
	// The inverse of the update case: on create there is nothing to narrow,
	// and sending empty collections would fight any server-side default.
	m := &policySetModel{
		Name:        types.StringValue("baseline"),
		AllowLabels: types.MapNull(types.ListType{ElemType: types.StringType}),
	}

	if got := buildCreateRequest(m).AllowLabels; got != nil {
		t.Fatalf("allow_labels = %v, want nil on create", got)
	}
}

func TestEnabledDefaultsTrueWhenUnset(t *testing.T) {
	// A set authored without `enabled` should be evaluated. Reading the zero
	// value of the bool would create a set that silently does nothing.
	m := &policySetModel{Name: types.StringValue("baseline"), Enabled: types.BoolNull()}
	if !buildCreateRequest(m).Enabled {
		t.Fatal("a set with no `enabled` in the configuration must be enabled")
	}

	m.Enabled = types.BoolValue(false)
	if buildCreateRequest(m).Enabled {
		t.Fatal("an explicit enabled=false must be honoured")
	}
}

func TestAnUnsetOptionalStaysNullAfterRead(t *testing.T) {
	// The server answers an absent optional with "". Storing that turns every
	// subsequent plan into a null -> "" change that never converges.
	m := &policySetModel{
		Description: types.StringNull(),
		VCSRepoURL:  types.StringNull(),
	}
	ps := &terrapod.PolicySet{ID: "ps-1", Name: "baseline", Source: "inline"}

	if d := readFromSDK(context.Background(), ps, m); d.HasError() {
		t.Fatalf("read: %v", d)
	}

	if !m.Description.IsNull() {
		t.Fatalf("description = %q, want null", m.Description.ValueString())
	}
	if !m.VCSRepoURL.IsNull() {
		t.Fatalf("vcs_repo_url = %q, want null", m.VCSRepoURL.ValueString())
	}
	if !m.AllowLabels.IsNull() {
		t.Fatal("allow_labels should stay null when the server has none")
	}
}

func TestAServerValueIsStoredEvenWhenTheConfigSaidNothing(t *testing.T) {
	// The null-preserving rule must not hide real drift: a description set
	// outside Terraform has to show up.
	m := &policySetModel{Description: types.StringNull()}
	ps := &terrapod.PolicySet{ID: "ps-1", Description: "set in the UI", Source: "inline"}

	if d := readFromSDK(context.Background(), ps, m); d.HasError() {
		t.Fatalf("read: %v", d)
	}
	if m.Description.ValueString() != "set in the UI" {
		t.Fatalf("description = %q, want the server's value", m.Description.ValueString())
	}
}

func TestTheConnectionIDKeepsTheFormTheConfigWrote(t *testing.T) {
	// The endpoint serialises it bare while a data source's `.id` is prefixed,
	// and a configuration naturally interpolates the latter. Storing the
	// server's spelling verbatim is #1748, which failed every apply with
	// "Provider produced inconsistent result after apply".
	m := &policySetModel{VCSConnectionID: types.StringValue("vcs-abc123")}
	ps := &terrapod.PolicySet{ID: "ps-1", Source: "vcs", VCSConnectionID: "abc123"}

	if d := readFromSDK(context.Background(), ps, m); d.HasError() {
		t.Fatalf("read: %v", d)
	}
	if got := m.VCSConnectionID.ValueString(); got != "vcs-abc123" {
		t.Fatalf("vcs_connection_id = %q, want the configured prefixed form", got)
	}
}

func TestTheConnectionIDFollowsTheServerOnRealDrift(t *testing.T) {
	m := &policySetModel{VCSConnectionID: types.StringValue("vcs-abc123")}
	ps := &terrapod.PolicySet{ID: "ps-1", Source: "vcs", VCSConnectionID: "def456"}

	if d := readFromSDK(context.Background(), ps, m); d.HasError() {
		t.Fatalf("read: %v", d)
	}
	if got := m.VCSConnectionID.ValueString(); got != "def456" {
		t.Fatalf("vcs_connection_id = %q, want the server's on a genuine change", got)
	}
}

func TestReadCarriesTheSyncStatus(t *testing.T) {
	// A VCS-backed set that cannot sync is evaluated against whatever it last
	// managed to fetch, so the error has to be visible in state.
	m := &policySetModel{}
	ps := &terrapod.PolicySet{
		ID:           "ps-1",
		Source:       "vcs",
		PolicyCount:  4,
		VCSLastError: "authentication failed",
	}

	if d := readFromSDK(context.Background(), ps, m); d.HasError() {
		t.Fatalf("read: %v", d)
	}
	if m.VCSLastError.ValueString() != "authentication failed" {
		t.Fatalf("vcs_last_error = %q", m.VCSLastError.ValueString())
	}
	if m.PolicyCount.ValueInt64() != 4 {
		t.Fatalf("policy_count = %d, want 4", m.PolicyCount.ValueInt64())
	}
}

// A name-scoped set that is imported, then applied, used to lose its scope.
//
// `readFromSDK` never populated AllowNames/DenyNames (go-terrapod did not even
// decode them), so after `terraform import` both read as null. The next apply
// called buildUpdateRequest, whose `sliceFromTFListOrEmpty` turns a null list
// into a NON-NIL empty slice, and policySetUpdateAttrs sends any non-nil
// slice — so the PATCH carried `"allow-names": []` and erased the rule. No
// plan showed it beforehand, because Read had never populated the value there
// was a diff against.
func TestReadPopulatesNameRulesSoImportDoesNotEraseThem(t *testing.T) {
	ctx := context.Background()
	// The shape `terraform import` produces: nothing configured locally yet.
	m := &policySetModel{}
	ps := &terrapod.PolicySet{
		ID:         "pset-1",
		Name:       "prod-guardrails",
		AllowNames: []string{"prod-network", "prod-data"},
		DenyNames:  []string{"sandbox"},
	}

	if d := readFromSDK(ctx, ps, m); d.HasError() {
		t.Fatalf("readFromSDK: %v", d)
	}

	if m.AllowNames.IsNull() {
		t.Fatal("allow_names read back null — the next apply would PATCH it to [] and erase the scope")
	}
	got := sliceFromTFList(m.AllowNames)
	if len(got) != 2 || got[0] != "prod-network" || got[1] != "prod-data" {
		t.Errorf("allow_names = %v, want [prod-network prod-data]", got)
	}
	if deny := sliceFromTFList(m.DenyNames); len(deny) != 1 || deny[0] != "sandbox" {
		t.Errorf("deny_names = %v, want [sandbox]", deny)
	}
}

// The other half: a set with genuinely no name scoping must stay null rather
// than becoming an empty list, or every plan shows a permanent null -> [] diff.
func TestAnUnscopedSetKeepsItsNameRulesNull(t *testing.T) {
	ctx := context.Background()
	m := &policySetModel{}

	if d := readFromSDK(ctx, &terrapod.PolicySet{ID: "pset-2", Name: "global"}, m); d.HasError() {
		t.Fatalf("readFromSDK: %v", d)
	}
	if !m.AllowNames.IsNull() || !m.DenyNames.IsNull() {
		t.Error("an unscoped set read back non-null name rules — that plans forever")
	}
}
