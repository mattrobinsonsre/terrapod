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
