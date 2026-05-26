package atlantis

import (
	"errors"
	"strings"
	"testing"
)

func TestParse_MinimalV3(t *testing.T) {
	// The smallest valid atlantis.yaml — version + one project with a
	// single field. Real-world starter examples look like this.
	in := `
version: 3
projects:
  - dir: infra/prod
`
	got, err := Parse("test.yaml", []byte(in))
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if got.Version != 3 || len(got.Projects) != 1 || got.Projects[0].Dir != "infra/prod" {
		t.Errorf("unexpected parse: %+v", got)
	}
}

func TestParse_RealisticDocument(t *testing.T) {
	// Cover the common shape: named project, autoplan, terraform_version,
	// apply_requirements (which we record as advisory), and a tofu
	// distribution toggle (which translates directly to Terrapod's
	// execution_backend).
	in := `
version: 3
automerge: true
projects:
  - name: api-prod
    dir: services/api
    workspace: prod
    terraform_version: 1.11.4
    terraform_distribution: tofu
    autoplan:
      enabled: true
      when_modified:
        - "*.tf"
        - "../shared/*.tf"
    apply_requirements:
      - approved
      - mergeable
  - dir: services/web
    branch: /main/
workflows:
  custom-policy:
    plan:
      steps:
        - init
        - plan
    apply:
      steps:
        - run: make migrate
        - apply
`
	got, err := Parse("acme/atlantis.yaml", []byte(in))
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if len(got.Projects) != 2 {
		t.Fatalf("expected 2 projects, got %d", len(got.Projects))
	}
	api := got.Projects[0]
	if api.Name != "api-prod" || api.Dir != "services/api" || api.Workspace != "prod" {
		t.Errorf("api project not parsed correctly: %+v", api)
	}
	if api.TerraformVersion != "1.11.4" || api.TerraformDistribution != "tofu" {
		t.Errorf("api project version/dist fields: %+v", api)
	}
	if !api.AutoPlan.Enabled || len(api.AutoPlan.WhenModified) != 2 {
		t.Errorf("api autoplan: %+v", api.AutoPlan)
	}
	if len(api.ApplyRequirements) != 2 || api.ApplyRequirements[0] != "approved" {
		t.Errorf("api apply_requirements: %+v", api.ApplyRequirements)
	}
	web := got.Projects[1]
	if web.Name != "" || web.Dir != "services/web" || web.Branch != "/main/" {
		t.Errorf("web project not parsed correctly: %+v", web)
	}
	if _, ok := got.Workflows["custom-policy"]; !ok {
		t.Errorf("workflows map missing 'custom-policy': %+v", got.Workflows)
	}
}

func TestParse_AutoDiscoverBlock(t *testing.T) {
	// Atlantis v0.27+ AutoDiscover. The emitter uses this hint to
	// decide whether to lean on Terrapod's autodiscovery rules.
	in := `
version: 3
autodiscover:
  mode: enabled
  ignore_paths:
    - tests/**
    - examples/**
projects:
  - dir: services/api
`
	got, err := Parse("test.yaml", []byte(in))
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if got.AutoDiscover == nil {
		t.Fatal("AutoDiscover should be non-nil when present")
	}
	if got.AutoDiscover.Mode != "enabled" || len(got.AutoDiscover.IgnorePaths) != 2 {
		t.Errorf("AutoDiscover: %+v", got.AutoDiscover)
	}
}

func TestParse_RejectsVersion1(t *testing.T) {
	// Atlantis itself refuses v1/v2 in modern releases. Mirror that
	// so an operator with a legacy file sees a clear error rather
	// than a half-parsed plan against a schema we don't model.
	in := `
version: 1
projects:
  - dir: infra
`
	_, err := Parse("legacy.yaml", []byte(in))
	if !errors.Is(err, ErrUnsupportedVersion) {
		t.Errorf("expected ErrUnsupportedVersion, got: %v", err)
	}
	if !strings.Contains(err.Error(), "legacy.yaml") {
		t.Errorf("error should name the file, got: %v", err)
	}
}

func TestParse_RejectsVersion2(t *testing.T) {
	in := `
version: 2
projects:
  - dir: infra
`
	_, err := Parse("test.yaml", []byte(in))
	if !errors.Is(err, ErrUnsupportedVersion) {
		t.Errorf("expected ErrUnsupportedVersion, got: %v", err)
	}
}

func TestParse_MissingVersion(t *testing.T) {
	// Atlantis treats a missing `version:` as legacy v1; we surface
	// it as a distinct error so the message can name the required
	// field rather than just "unsupported".
	in := `
projects:
  - dir: infra
`
	_, err := Parse("test.yaml", []byte(in))
	if !errors.Is(err, ErrMissingVersion) {
		t.Errorf("expected ErrMissingVersion, got: %v", err)
	}
}

func TestParse_EmptyFile(t *testing.T) {
	_, err := Parse("test.yaml", nil)
	if err == nil {
		t.Fatal("expected error on empty file")
	}
	if !strings.Contains(err.Error(), "empty file") {
		t.Errorf("error should say 'empty file', got: %v", err)
	}
}

func TestParse_ProjectWithoutDir(t *testing.T) {
	// Atlantis itself rejects this; we mirror to catch it at migration
	// time rather than at first-run time.
	in := `
version: 3
projects:
  - name: oops
`
	_, err := Parse("bad.yaml", []byte(in))
	if err == nil {
		t.Fatal("expected error on project without dir")
	}
	if !strings.Contains(err.Error(), `"oops"`) || !strings.Contains(err.Error(), "missing `dir`") {
		t.Errorf("error should name the project and say 'missing dir', got: %v", err)
	}
}

func TestParse_UnknownFieldsAreIgnored(t *testing.T) {
	// Forward-compatibility: a newer v3 doc with a field we don't
	// know about (e.g. a future Atlantis adds `policy_label:`) should
	// still parse. We use KnownFields(false).
	in := `
version: 3
some_future_top_level_field: yes
projects:
  - dir: infra
    some_future_project_field: yes
`
	got, err := Parse("test.yaml", []byte(in))
	if err != nil {
		t.Fatalf("forward-compat parse failed: %v", err)
	}
	if got.Projects[0].Dir != "infra" {
		t.Errorf("known fields still parsed: %+v", got.Projects[0])
	}
}

func TestProjectIdentifier(t *testing.T) {
	cases := []struct {
		name string
		p    Project
		want string
	}{
		{"explicit name takes priority", Project{Name: "api-prod", Dir: "services/api"}, "api-prod"},
		{"derive from dir when name empty", Project{Dir: "services/api"}, "services/api"},
		{"derive from dir + workspace when set non-default", Project{Dir: "services/api", Workspace: "prod"}, "services/api/prod"},
		{"derive from dir when workspace is 'default'", Project{Dir: "services/api", Workspace: "default"}, "services/api"},
		{"dot-dir fallback", Project{}, "."},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := ProjectIdentifier(c.p); got != c.want {
				t.Errorf("ProjectIdentifier(%+v) = %q, want %q", c.p, got, c.want)
			}
		})
	}
}

func TestParse_MalformedYAML(t *testing.T) {
	in := `
version: 3
projects: [bad
`
	_, err := Parse("test.yaml", []byte(in))
	if err == nil {
		t.Fatal("expected error on malformed yaml")
	}
	if !strings.Contains(err.Error(), "parse YAML") {
		t.Errorf("error should mention 'parse YAML', got: %v", err)
	}
}
