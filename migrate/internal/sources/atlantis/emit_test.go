package atlantis

import (
	"strings"
	"testing"
)

const testRepo = "https://github.com/acme/infra"

func mustParse(t *testing.T, in string) *AtlantisYAML {
	t.Helper()
	doc, err := Parse("test.yaml", []byte(in))
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	return doc
}

func TestEmit_SingleProject(t *testing.T) {
	doc := mustParse(t, `
version: 3
projects:
  - dir: infra/prod
    terraform_version: 1.12.0
`)
	ws, _, err := Emit(doc, EmitOptions{
		Repo:             testRepo,
		VCSConnectionRef: "atlantis-github",
		DefaultBranch:    "main",
	})
	if err != nil {
		t.Fatalf("Emit: %v", err)
	}
	if len(ws) != 1 {
		t.Fatalf("expected 1 workspace, got %d", len(ws))
	}
	got := ws[0]
	if got.SourceID != "infra/prod" {
		t.Errorf("SourceID = %q, want %q", got.SourceID, "infra/prod")
	}
	if got.Name != "infra-prod" {
		t.Errorf("Name = %q, want %q (slash replaced)", got.Name, "infra-prod")
	}
	if got.ExecutionMode != "agent" {
		t.Errorf("ExecutionMode = %q, want %q (Atlantis migrations are always agent-mode)", got.ExecutionMode, "agent")
	}
	if got.WorkingDirectory != "infra/prod" {
		t.Errorf("WorkingDirectory = %q, want %q", got.WorkingDirectory, "infra/prod")
	}
	if got.TerraformVersion != "1.12.0" {
		t.Errorf("TerraformVersion = %q, want %q", got.TerraformVersion, "1.12.0")
	}
	if got.VCSRepoURL != testRepo {
		t.Errorf("VCSRepoURL = %q, want %q", got.VCSRepoURL, testRepo)
	}
	if got.VCSConnectionRef != "atlantis-github" {
		t.Errorf("VCSConnectionRef = %q, want %q", got.VCSConnectionRef, "atlantis-github")
	}
	if got.VCSBranch != "main" {
		t.Errorf("VCSBranch = %q, want %q (default applied)", got.VCSBranch, "main")
	}
}

func TestEmit_NamedProjectWithCustomWorkflow(t *testing.T) {
	// Named projects keep their name as SourceID. Custom-workflow
	// reference emits a SkippedItem so the operator knows the
	// workflow body needs manual translation.
	doc := mustParse(t, `
version: 3
projects:
  - name: api-prod
    dir: services/api
    workspace: prod
    terraform_version: 1.12.0
    workflow: custom-policy
workflows:
  custom-policy:
    apply:
      steps:
        - run: make migrate
        - apply
`)
	ws, skipped, err := Emit(doc, EmitOptions{Repo: testRepo, DefaultBranch: "main"})
	if err != nil {
		t.Fatalf("Emit: %v", err)
	}
	if len(ws) != 1 || ws[0].SourceID != "api-prod" {
		t.Fatalf("SourceID = %+v", ws)
	}
	if got := ws[0].Labels["terrapod-migration/atlantis-workspace"]; got != "prod" {
		t.Errorf("workspace label = %q, want %q", got, "prod")
	}
	// One skipped-item for the workflow reference; the workflow
	// definition itself should NOT also fire a "defined but
	// unreferenced" item (we de-dup by name).
	if len(skipped) != 1 {
		t.Fatalf("expected 1 skipped-item, got %d: %+v", len(skipped), skipped)
	}
	if skipped[0].Kind != "atlantis-workflow" || !strings.Contains(skipped[0].Name, "custom-policy") {
		t.Errorf("unexpected SkippedItem: %+v", skipped[0])
	}
}

func TestEmit_RejectsDuplicateIdentifiers(t *testing.T) {
	// Two projects deriving the same SourceID (same dir+workspace) →
	// hard error. Atlantis disambiguates via `name:`; the migration
	// tool surfaces the ambiguity rather than silently overwriting
	// one workspace with the other.
	doc := mustParse(t, `
version: 3
projects:
  - dir: services/api
  - dir: services/api
`)
	_, _, err := Emit(doc, EmitOptions{Repo: testRepo})
	if err == nil {
		t.Fatal("expected duplicate-identifier error")
	}
	if !strings.Contains(err.Error(), "duplicate project identifier") {
		t.Errorf("error should mention duplicate-identifier, got: %v", err)
	}
}

func TestEmit_BranchRegexStripped(t *testing.T) {
	// Atlantis `branch:` is a regex, often written /main/ — strip the
	// delimiters and use the literal name for Terrapod's vcs_branch.
	doc := mustParse(t, `
version: 3
projects:
  - dir: services/a
    branch: /main/
  - dir: services/b
`)
	ws, _, err := Emit(doc, EmitOptions{Repo: testRepo, DefaultBranch: "trunk"})
	if err != nil {
		t.Fatalf("Emit: %v", err)
	}
	if ws[0].VCSBranch != "main" {
		t.Errorf("regex-form branch: got %q, want %q", ws[0].VCSBranch, "main")
	}
	if ws[1].VCSBranch != "trunk" {
		t.Errorf("default-branch fallback: got %q, want %q", ws[1].VCSBranch, "trunk")
	}
}

func TestEmit_ApplyRequirementsSkippedItem(t *testing.T) {
	doc := mustParse(t, `
version: 3
projects:
  - dir: a
    apply_requirements: [approved, mergeable]
`)
	_, skipped, err := Emit(doc, EmitOptions{Repo: testRepo})
	if err != nil {
		t.Fatalf("Emit: %v", err)
	}
	if len(skipped) != 1 || skipped[0].Kind != "atlantis-apply-requirements" {
		t.Fatalf("expected one apply-requirements SkippedItem, got %+v", skipped)
	}
	if !strings.Contains(skipped[0].Name, "approved, mergeable") {
		t.Errorf("SkippedItem.Name should list requirements: %+v", skipped[0])
	}
}

func TestEmit_CustomPolicyCheckSkippedItem(t *testing.T) {
	truth := true
	doc := mustParse(t, `version: 3
projects:
  - dir: a
    custom_policy_check: true
`)
	// Sanity check the parser captured the *bool
	if doc.Projects[0].CustomPolicyCheck == nil || *doc.Projects[0].CustomPolicyCheck != truth {
		t.Fatalf("parser didn't set *bool: %+v", doc.Projects[0].CustomPolicyCheck)
	}
	_, skipped, err := Emit(doc, EmitOptions{Repo: testRepo})
	if err != nil {
		t.Fatalf("Emit: %v", err)
	}
	if len(skipped) != 1 || skipped[0].Kind != "atlantis-custom-policy-check" {
		t.Errorf("expected custom-policy-check skipped-item, got %+v", skipped)
	}
}

func TestEmit_ExecutionOrderGroupSkippedItem(t *testing.T) {
	doc := mustParse(t, `version: 3
projects:
  - dir: a
    execution_order_group: 5
`)
	_, skipped, err := Emit(doc, EmitOptions{Repo: testRepo})
	if err != nil {
		t.Fatalf("Emit: %v", err)
	}
	if len(skipped) != 1 || skipped[0].Kind != "atlantis-execution-order-group" {
		t.Errorf("expected execution-order-group skipped-item, got %+v", skipped)
	}
}

func TestEmit_TerraformDistributionLabel(t *testing.T) {
	// `terraform_distribution: tofu` is captured as a label so a
	// later increment of the writer (Terrapod execution_backend
	// field) can promote it without parser changes.
	doc := mustParse(t, `version: 3
projects:
  - dir: a
    terraform_distribution: tofu
`)
	ws, _, err := Emit(doc, EmitOptions{Repo: testRepo})
	if err != nil {
		t.Fatalf("Emit: %v", err)
	}
	if got := ws[0].Labels["terrapod-migration/atlantis-distribution"]; got != "tofu" {
		t.Errorf("distribution label = %q, want %q", got, "tofu")
	}
}

func TestEmit_UnreferencedWorkflowSurfaced(t *testing.T) {
	// A workflow defined but not used by any project should still
	// appear in the report so operators don't lose track of it.
	doc := mustParse(t, `version: 3
projects:
  - dir: a
workflows:
  unused:
    plan:
      steps: [init, plan]
`)
	_, skipped, err := Emit(doc, EmitOptions{Repo: testRepo})
	if err != nil {
		t.Fatalf("Emit: %v", err)
	}
	if len(skipped) != 1 || !strings.Contains(skipped[0].Name, "unused") || !strings.Contains(skipped[0].Name, "unreferenced") {
		t.Errorf("expected unused-workflow skipped-item, got %+v", skipped)
	}
}

func TestEmit_RequiresRepo(t *testing.T) {
	doc := mustParse(t, `version: 3
projects: [{dir: a}]
`)
	_, _, err := Emit(doc, EmitOptions{})
	if err == nil {
		t.Fatal("expected error when Repo is missing")
	}
	if !strings.Contains(err.Error(), "Repo is required") {
		t.Errorf("error should mention missing Repo, got: %v", err)
	}
}

func TestEmit_NilDoc(t *testing.T) {
	_, _, err := Emit(nil, EmitOptions{Repo: testRepo})
	if err == nil {
		t.Fatal("expected error for nil doc")
	}
}

func TestTerrapodWorkspaceName(t *testing.T) {
	cases := []struct{ in, want string }{
		{"infra/prod", "infra-prod"},
		{"api-prod", "api-prod"},
		{"a/b/c", "a-b-c"},
		{".", "."},
	}
	for _, c := range cases {
		if got := terrapodWorkspaceName(c.in); got != c.want {
			t.Errorf("terrapodWorkspaceName(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestBranchOrDefault(t *testing.T) {
	cases := []struct{ project, dflt, want string }{
		{"", "main", "main"},
		{"release", "main", "release"},
		{"/main/", "trunk", "main"},
		{"/release-.*/", "main", "release-.*"}, // Pattern returned as-is — operator handles
		{"  /main/  ", "trunk", "main"},        // Whitespace tolerated
	}
	for _, c := range cases {
		if got := branchOrDefault(c.project, c.dflt); got != c.want {
			t.Errorf("branchOrDefault(%q, %q) = %q, want %q", c.project, c.dflt, got, c.want)
		}
	}
}
