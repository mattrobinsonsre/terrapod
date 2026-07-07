package tfe

import (
	"net/http"
	"strings"
	"testing"

	"github.com/hashicorp/go-tfe"

	"github.com/mattrobinsonsre/terrapod/migrate/internal/ir"
)

// varListBody builds a TFE workspace-variables list response.
func varListBody(items ...string) string {
	return `{
  "data": [` + strings.Join(items, ",\n") + `],
  "meta": {"pagination": {"current-page": 1, "total-pages": 1, "total-count": ` + intLen(items) + `}}
}`
}

// varItem assembles one variable in the list response shape.
func varItem(id, key, value, category string, sensitive, hcl bool) string {
	sb, hb := "false", "false"
	if sensitive {
		sb = "true"
	}
	if hcl {
		hb = "true"
	}
	return `{
    "id": "` + id + `",
    "type": "vars",
    "attributes": {
      "key": "` + key + `",
      "value": "` + value + `",
      "category": "` + category + `",
      "sensitive": ` + sb + `,
      "hcl": ` + hb + `,
      "description": ""
    }
  }`
}

func TestAttachVariables_TerraformAndEnvCategories(t *testing.T) {
	// Smoke the two-category translation: TF_VAR / env both flow
	// through with their flags preserved.
	f := newFakeTFE(t)
	f.orgRead("acme", http.StatusOK, minimalOrgBody)
	f.orgMembershipsList("acme", http.StatusOK)
	f.mux.HandleFunc("/api/v2/workspaces/ws-aaa/vars", func(w http.ResponseWriter, r *http.Request) {
		body := varListBody(
			varItem("var-1", "region", "eu-west-1", "terraform", false, false),
			// HCL var with a simple value — fixtures that need
			// embedded quotes would have to escape them per JSON
			// rules; not worth the readability hit for this test.
			varItem("var-2", "instance_count", "3", "terraform", false, true),
			varItem("var-3", "AWS_PROFILE", "ci", "env", false, false),
		)
		_, _ = w.Write([]byte(body))
	})

	c, err := NewClient(t.Context(), Config{Address: f.server.URL, Token: "t", OrgName: "acme"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	ws := []ir.Workspace{{SourceID: "ws-aaa", Name: "api-prod"}}
	skipped, err := c.AttachVariables(t.Context(), ws)
	if err != nil {
		t.Fatalf("AttachVariables: %v", err)
	}
	if len(ws[0].Variables) != 3 {
		t.Fatalf("expected 3 vars, got %d: %+v", len(ws[0].Variables), ws[0].Variables)
	}
	region := ws[0].Variables[0]
	if region.Key != "region" || region.Value != "eu-west-1" || region.Category != "terraform" || region.HCL {
		t.Errorf("region var: %+v", region)
	}
	hclVar := ws[0].Variables[1]
	if !hclVar.HCL {
		t.Errorf("HCL var should have HCL=true: %+v", hclVar)
	}
	envVar := ws[0].Variables[2]
	if envVar.Category != "env" {
		t.Errorf("env var category: %q", envVar.Category)
	}
	if len(skipped) != 0 {
		t.Errorf("no skipped items expected for non-sensitive non-dynamic vars: %+v", skipped)
	}
}

func TestAttachVariables_DynamicCredentialsStrippedAndReported(t *testing.T) {
	// TFE Dynamic Credentials env vars are stripped from the IR and
	// each one gets a SkippedItem with operator guidance.
	f := newFakeTFE(t)
	f.orgRead("acme", http.StatusOK, minimalOrgBody)
	f.orgMembershipsList("acme", http.StatusOK)
	f.mux.HandleFunc("/api/v2/workspaces/ws-aaa/vars", func(w http.ResponseWriter, r *http.Request) {
		body := varListBody(
			varItem("var-1", "TFC_AWS_PROVIDER_AUTH", "true", "env", false, false),
			varItem("var-2", "TFC_AWS_RUN_ROLE_ARN", "arn:aws:iam::123:role/x", "env", false, false),
			varItem("var-3", "region", "eu-west-1", "terraform", false, false),
		)
		_, _ = w.Write([]byte(body))
	})

	c, _ := NewClient(t.Context(), Config{Address: f.server.URL, Token: "t", OrgName: "acme"})
	ws := []ir.Workspace{{SourceID: "ws-aaa", Name: "api-prod"}}
	skipped, err := c.AttachVariables(t.Context(), ws)
	if err != nil {
		t.Fatalf("AttachVariables: %v", err)
	}
	// Only the non-TFC variable should be in the IR.
	if len(ws[0].Variables) != 1 || ws[0].Variables[0].Key != "region" {
		t.Errorf("non-TFC vars only: %+v", ws[0].Variables)
	}
	// Two skipped-items for the two TFC_* env vars.
	dynCount := 0
	for _, s := range skipped {
		if s.Kind == "tfe-dynamic-credentials" {
			dynCount++
		}
	}
	if dynCount != 2 {
		t.Errorf("expected 2 tfe-dynamic-credentials skipped-items, got %d: %+v", dynCount, skipped)
	}
}

func TestAttachVariables_SensitiveVarReportsOwnerTier(t *testing.T) {
	// With an owner token, sensitive variables still get reported
	// (we don't migrate values to the IR for safety) but the
	// guidance reads "re-enter manually" rather than mentioning
	// re-running with a higher tier.
	f := newFakeTFE(t)
	f.orgRead("acme", http.StatusOK, minimalOrgBody)
	f.orgMembershipsList("acme", http.StatusOK)
	f.mux.HandleFunc("/api/v2/workspaces/ws-aaa/vars", func(w http.ResponseWriter, r *http.Request) {
		body := varListBody(
			varItem("var-1", "db_password", "", "terraform", true, false),
		)
		_, _ = w.Write([]byte(body))
	})

	c, _ := NewClient(t.Context(), Config{Address: f.server.URL, Token: "t", OrgName: "acme"})
	ws := []ir.Workspace{{SourceID: "ws-aaa", Name: "api-prod"}}
	skipped, err := c.AttachVariables(t.Context(), ws)
	if err != nil {
		t.Fatalf("AttachVariables: %v", err)
	}
	if len(ws[0].Variables) != 1 || !ws[0].Variables[0].Sensitive {
		t.Errorf("sensitive var should still be in IR with empty value: %+v", ws[0].Variables)
	}
	if len(skipped) != 1 || skipped[0].Kind != "tfe-sensitive-variable" {
		t.Fatalf("expected 1 sensitive SkippedItem, got %+v", skipped)
	}
	if !strings.Contains(skipped[0].Reason, "Re-enter manually") {
		t.Errorf("owner-tier reason should say 'Re-enter manually': %v", skipped[0])
	}
}

func TestAttachVariables_SensitiveVarReportsWorkerTier(t *testing.T) {
	// With a worker token, the guidance specifically suggests rerunning
	// with an owner token to read sensitive values automatically.
	f := newFakeTFE(t)
	f.orgRead("acme", http.StatusOK, minimalOrgBody)
	f.orgMembershipsList("acme", http.StatusForbidden) // → worker tier
	f.mux.HandleFunc("/api/v2/workspaces/ws-aaa/vars", func(w http.ResponseWriter, r *http.Request) {
		body := varListBody(varItem("var-1", "db_password", "", "terraform", true, false))
		_, _ = w.Write([]byte(body))
	})

	c, _ := NewClient(t.Context(), Config{Address: f.server.URL, Token: "t", OrgName: "acme"})
	if c.TokenTier != TokenTierWorker {
		t.Fatalf("expected worker tier, got %q", c.TokenTier)
	}
	ws := []ir.Workspace{{SourceID: "ws-aaa", Name: "api-prod"}}
	skipped, _ := c.AttachVariables(t.Context(), ws)
	if len(skipped) != 1 {
		t.Fatalf("expected 1 SkippedItem, got %+v", skipped)
	}
	if !strings.Contains(skipped[0].Reason, "worker-tier") {
		t.Errorf("worker-tier reason should call out the tier: %v", skipped[0])
	}
}

func TestVarsetToIR_Translation(t *testing.T) {
	vs := &tfe.VariableSet{
		ID:          "varset-1",
		Name:        "global-tags",
		Description: "shared",
		Global:      false,
		Priority:    true,
		Variables: []*tfe.VariableSetVariable{
			{Key: "environment", Value: "prod", Category: tfe.CategoryTerraform},
			{Key: "api_key", Value: "", Category: tfe.CategoryEnv, Sensitive: true},
			// Dynamic-credentials env var → stripped + reported.
			{Key: "TFC_AWS_PROVIDER_AUTH", Value: "true", Category: tfe.CategoryEnv},
		},
		Workspaces: []*tfe.Workspace{{ID: "ws-a"}, {ID: "ws-b"}},
	}

	set, skipped := varsetToIR(vs, TokenTierOwner)

	if set.SourceID != "varset-1" || set.Name != "global-tags" || !set.Priority || set.Global {
		t.Errorf("varset meta: %+v", set)
	}
	// Dynamic-creds var stripped → 2 remaining.
	if len(set.Variables) != 2 {
		t.Fatalf("expected 2 vars (dynamic-creds stripped), got %d: %+v", len(set.Variables), set.Variables)
	}
	var sawSensitive bool
	for _, v := range set.Variables {
		if v.Key == "api_key" {
			sawSensitive = true
			if v.Value != "" || !v.Sensitive {
				t.Errorf("sensitive var should be empty + sensitive: %+v", v)
			}
		}
	}
	if !sawSensitive {
		t.Error("api_key not translated")
	}
	if len(set.WorkspaceRefs) != 2 || set.WorkspaceRefs[0] != "ws-a" {
		t.Errorf("workspace refs: %+v", set.WorkspaceRefs)
	}
	var dyn, sens int
	for _, s := range skipped {
		switch s.Kind {
		case "tfe-dynamic-credentials":
			dyn++
		case "tfe-sensitive-varset-variable":
			sens++
		}
	}
	if dyn != 1 || sens != 1 {
		t.Errorf("skipped kinds: dyn=%d sens=%d (%+v)", dyn, sens, skipped)
	}
}

func TestVarsetToIR_GlobalHasNoWorkspaceRefs(t *testing.T) {
	vs := &tfe.VariableSet{
		ID: "vs-g", Name: "global", Global: true,
		Workspaces: []*tfe.Workspace{{ID: "ws-a"}},
	}
	set, _ := varsetToIR(vs, TokenTierOwner)
	if len(set.WorkspaceRefs) != 0 {
		t.Errorf("global varset should carry no workspace refs, got %+v", set.WorkspaceRefs)
	}
}

func TestVarsetToIR_ProjectScopeReported(t *testing.T) {
	vs := &tfe.VariableSet{
		ID: "vs-p", Name: "proj",
		Projects: []*tfe.Project{{ID: "prj-1"}},
	}
	_, skipped := varsetToIR(vs, TokenTierOwner)
	var found bool
	for _, s := range skipped {
		if s.Kind == "tfe-variable-set-project-scope" {
			found = true
		}
	}
	if !found {
		t.Errorf("project-scoped varset should emit a skipped item: %+v", skipped)
	}
}

func TestIsDynamicCredsKey(t *testing.T) {
	cases := []struct {
		key  string
		want bool
	}{
		{"TFC_AWS_PROVIDER_AUTH", true},
		{"TFC_GCP_PROVIDER_AUTH", true},
		{"TFC_AZURE_RUN_CLIENT_ID", true},
		{"TFC_VAULT_ADDR", true},
		// Not in our list — operator's own TFC_-prefixed var, leave alone.
		{"TFC_AWS_PROVIDER_AUTH_FOO", false},
		{"TFC_CUSTOM_THING", false},
		// Non-TFC prefixes never match.
		{"AWS_PROFILE", false},
		{"DATABASE_URL", false},
	}
	for _, c := range cases {
		if got := isDynamicCredsKey(c.key); got != c.want {
			t.Errorf("isDynamicCredsKey(%q) = %v, want %v", c.key, got, c.want)
		}
	}
}

func TestStripTFCPrefixedVariables(t *testing.T) {
	// Public helper called by the Terrapod writer as a defence-in-depth.
	// Should drop only known TFC_ env vars; leave TFC_-prefixed terraform
	// vars (rare; would be in a TF_VAR_TFC_... naming) and unknown TFC_
	// env names alone.
	in := []ir.Variable{
		{Key: "region", Category: "terraform"},
		{Key: "TFC_AWS_PROVIDER_AUTH", Category: "env"},
		{Key: "TFC_CUSTOM_THING", Category: "env"},
		{Key: "DATABASE_URL", Category: "env"},
	}
	out := StripTFCPrefixedVariables(in)
	wantKeys := []string{"region", "TFC_CUSTOM_THING", "DATABASE_URL"}
	if len(out) != len(wantKeys) {
		t.Fatalf("StripTFCPrefixedVariables: got %d, want %d: %+v", len(out), len(wantKeys), out)
	}
	for i, w := range wantKeys {
		if out[i].Key != w {
			t.Errorf("out[%d] = %+v, want %q", i, out[i], w)
		}
	}
}

func TestCategoryString(t *testing.T) {
	cases := []struct {
		in   tfe.CategoryType
		want string
	}{
		{tfe.CategoryTerraform, "terraform"},
		{tfe.CategoryEnv, "env"},
		{tfe.CategoryType("future-tfe-category"), "future-tfe-category"},
		{tfe.CategoryType(""), ""},
	}
	for _, c := range cases {
		if got := categoryString(c.in); got != c.want {
			t.Errorf("categoryString(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}
