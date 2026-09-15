package module_autodiscovery_rule

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

func TestRequestFromModelSendsConfiguredFieldsAndSkipsUnknown(t *testing.T) {
	ctx := context.Background()
	m := &moduleRuleModel{
		Name:            types.StringValue("mg"),
		VCSConnectionID: types.StringValue("vcs-1"),
		RepoURL:         types.StringValue("https://github.com/org/terraform-azurerm-mg"),
		Pattern:         types.StringValue("**/*.tf"),
		Branch:          types.StringValue(""),
		Enabled:         types.BoolValue(false),
		NameTemplate:    types.StringValue("{repo}-{leaf}"),
		Provider:        types.StringValue(""),
		VCSTagPattern:   types.StringValue("v*"),
		OwnerEmail:      types.StringValue(""),
		IgnorePatterns:  types.ListUnknown(types.StringType),
		Labels:          types.MapNull(types.StringType),
	}
	req := requestFromModel(ctx, m)
	if req.Name == nil || *req.Name != "mg" || req.Pattern == nil || *req.Pattern != "**/*.tf" {
		t.Errorf("required fields: %+v", req)
	}
	if req.Enabled == nil || *req.Enabled {
		t.Errorf("enabled=false must be sent: %+v", req.Enabled)
	}
	if req.Provider == nil || *req.Provider != "" {
		t.Error("an empty provider is configuration (derive from the repo name) and must be sent")
	}
	if req.IgnorePatterns != nil || req.Labels != nil {
		t.Errorf("unknown/null collections must not be sent: %+v %+v", req.IgnorePatterns, req.Labels)
	}
}

func TestRequestFromModelSendsAnEmptyListToClearIt(t *testing.T) {
	ctx := context.Background()
	empty, _ := types.ListValueFrom(ctx, types.StringType, []string{})
	m := &moduleRuleModel{IgnorePatterns: empty, Labels: types.MapNull(types.StringType)}
	req := requestFromModel(ctx, m)
	if req.IgnorePatterns == nil || len(*req.IgnorePatterns) != 0 {
		t.Errorf("a configured empty list must be sent as []: %+v", req.IgnorePatterns)
	}
}

func TestReadIntoModelKeepsTheConfiguredConnectionForm(t *testing.T) {
	ctx := context.Background()
	rule := &terrapod.ModuleAutodiscoveryRule{
		ID:              "modrule-1",
		Name:            "mg",
		VCSConnectionID: "vcs-2222",
		Pattern:         "**/*.tf",
		Enabled:         true,
		VCSTagPattern:   "v*",
		FirstScanAt:     "2026-09-14T10:00:00Z",
		LastScannedSHA:  "abc",
	}
	m := &moduleRuleModel{VCSConnectionID: types.StringValue("2222")}
	if d := readIntoModel(ctx, rule, m); d.HasError() {
		t.Fatal(d)
	}
	if m.VCSConnectionID.ValueString() != "2222" {
		t.Errorf("the bare configured id should be kept, got %q", m.VCSConnectionID.ValueString())
	}
	if m.IgnorePatterns.IsNull() || len(m.IgnorePatterns.Elements()) != 0 {
		t.Error("nil ignore patterns should read as an empty list")
	}
	if m.Labels.IsNull() || m.FirstScanAt.ValueString() != "2026-09-14T10:00:00Z" || m.ID.ValueString() != "modrule-1" {
		t.Errorf("model: %+v", m)
	}

	other := &moduleRuleModel{VCSConnectionID: types.StringValue("vcs-9999")}
	_ = readIntoModel(ctx, rule, other)
	if other.VCSConnectionID.ValueString() != "vcs-2222" {
		t.Errorf("a different connection must take the server's value, got %q", other.VCSConnectionID.ValueString())
	}
}

// The server trims strings, stores an empty tag pattern as "v*" and drops blank
// ignore patterns. Writing those tidied values over the configured ones failed
// the apply as an inconsistent result (#1634), so the configured form is kept.
func TestReadIntoModelKeepsConfiguredFormsTheServerNormalises(t *testing.T) {
	ctx := context.Background()
	rule := &terrapod.ModuleAutodiscoveryRule{
		ID:              "modrule-1",
		Name:            "mg",
		VCSConnectionID: "vcs-2222",
		RepoURL:         "https://github.com/org/terraform-azurerm-mg",
		Branch:          "main",
		Pattern:         "**/*.tf",
		IgnorePatterns:  []string{"examples/**"},
		Provider:        "azurerm",
		VCSTagPattern:   "v*",
		OwnerEmail:      "owner@example.com",
	}
	configuredIgnore, _ := types.ListValueFrom(ctx, types.StringType, []string{" examples/** ", "", "  "})
	m := &moduleRuleModel{
		Name:            types.StringValue(" mg "),
		VCSConnectionID: types.StringValue("vcs-2222"),
		RepoURL:         types.StringValue("https://github.com/org/terraform-azurerm-mg "),
		Branch:          types.StringValue(" main"),
		Pattern:         types.StringValue("**/*.tf "),
		IgnorePatterns:  configuredIgnore,
		Provider:        types.StringValue(" azurerm "),
		VCSTagPattern:   types.StringValue(""),
		OwnerEmail:      types.StringValue(" owner@example.com "),
		Labels:          types.MapNull(types.StringType),
	}
	if d := readIntoModel(ctx, rule, m); d.HasError() {
		t.Fatal(d)
	}
	for name, got := range map[string]types.String{
		" mg ":                m.Name,
		" main":               m.Branch,
		"**/*.tf ":            m.Pattern,
		" azurerm ":           m.Provider,
		"":                    m.VCSTagPattern,
		" owner@example.com ": m.OwnerEmail,
		"https://github.com/org/terraform-azurerm-mg ": m.RepoURL,
	} {
		if got.IsNull() || got.ValueString() != name {
			t.Errorf("configured %q should be kept, got %v", name, got)
		}
	}
	if !m.IgnorePatterns.Equal(configuredIgnore) {
		t.Errorf("ignore patterns that normalise to the server's should be kept, got %v", m.IgnorePatterns)
	}
}

func TestReadIntoModelCarriesTheOrgFields(t *testing.T) {
	ctx := context.Background()
	rule := &terrapod.ModuleAutodiscoveryRule{
		ID:               "modrule-1",
		RepoURL:          "https://github.com/org/terraform-*",
		TargetKind:       "pattern",
		LastEnumeratedAt: "2026-09-15T11:00:00Z",
		LastError:        "the org could not be listed",
		VCSTagPattern:    "v*",
	}
	m := &moduleRuleModel{Labels: types.MapNull(types.StringType)}
	if d := readIntoModel(ctx, rule, m); d.HasError() {
		t.Fatal(d)
	}
	if m.TargetKind.ValueString() != "pattern" || m.LastEnumeratedAt.ValueString() != "2026-09-15T11:00:00Z" ||
		m.LastError.ValueString() != "the org could not be listed" {
		t.Errorf("org fields: %+v", m)
	}

	// An older server sends none of them: known empty strings, never unknown,
	// so a refresh leaves nothing "known after apply".
	_ = readIntoModel(ctx, &terrapod.ModuleAutodiscoveryRule{ID: "modrule-1"}, m)
	for name, v := range map[string]types.String{"target_kind": m.TargetKind, "last_error": m.LastError, "last_enumerated_at": m.LastEnumeratedAt} {
		if v.IsNull() || v.IsUnknown() || v.ValueString() != "" {
			t.Errorf("%s should be a known empty string, got %v", name, v)
		}
	}
}

func TestRepoURLProblem(t *testing.T) {
	ok := []string{
		"https://github.com/org/terraform-aws-network",
		"https://github.com/org",
		"https://github.com/org/terraform-*",
		"https://github.com/org/terraform-[ab]?",
		"https://gitlab.com/group/sub/project",
		"https://gitlab.com/group/sub/terraform-*",
		"git@github.com:org/terraform-*.git",
		"org/terraform-*",
		"org",
		"https://github.com/org/",
	}
	for _, v := range ok {
		if msg := repoURLProblem(v); msg != "" {
			t.Errorf("%q should pass, got %q", v, msg)
		}
	}
	bad := []string{
		"https://github.com/*/terraform-aws",
		"https://gitlab.com/group/*/project",
		"https://gitlab.com/group/**/terraform-*",
		"git@github.com:or?/repo",
		"org*/repo",
		"   ",
	}
	for _, v := range bad {
		if repoURLProblem(v) == "" {
			t.Errorf("%q should be refused", v)
		}
	}
}

func TestReadIntoModelTakesTheServerValueWhenItDiffers(t *testing.T) {
	ctx := context.Background()
	rule := &terrapod.ModuleAutodiscoveryRule{
		ID:             "modrule-1",
		Branch:         "develop",
		IgnorePatterns: []string{"test/**"},
		VCSTagPattern:  "v*",
		OwnerEmail:     "",
	}
	configuredIgnore, _ := types.ListValueFrom(ctx, types.StringType, []string{"examples/**"})
	m := &moduleRuleModel{
		Branch:         types.StringValue("main"),
		IgnorePatterns: configuredIgnore,
		VCSTagPattern:  types.StringValue("release-*"),
		OwnerEmail:     types.StringValue("owner@example.com"),
		Labels:         types.MapNull(types.StringType),
	}
	_ = readIntoModel(ctx, rule, m)
	if m.Branch.ValueString() != "develop" || m.VCSTagPattern.ValueString() != "v*" || m.OwnerEmail.ValueString() != "" {
		t.Errorf("changed values must take the server's: %+v", m)
	}
	want, _ := types.ListValueFrom(ctx, types.StringType, []string{"test/**"})
	if !m.IgnorePatterns.Equal(want) {
		t.Errorf("changed ignore patterns must take the server's, got %v", m.IgnorePatterns)
	}
}
