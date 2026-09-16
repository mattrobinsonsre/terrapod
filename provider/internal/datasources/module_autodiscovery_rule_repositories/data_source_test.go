package module_autodiscovery_rule_repositories

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/datasource"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

func TestEntriesFromMapsEveryField(t *testing.T) {
	ctx := context.Background()
	entries, diags := entriesFrom(ctx, []terrapod.ModuleAutodiscoveryRepository{{
		ID:             "modrepo-1",
		Repository:     "org/terraform-aws-a",
		RepoURL:        "https://github.com/org/terraform-aws-a",
		DefaultBranch:  "main",
		Status:         "active",
		Origin:         "new",
		Candidates:     []terrapod.ModuleAutodiscoveryStoredCandidate{{Subdirectory: ""}, {Subdirectory: "modules/x"}},
		PreviousPaths:  []terrapod.ModuleAutodiscoveryRepositoryPrevious{{Path: "old/terraform-aws-a"}},
		LastScannedSHA: "abc",
		FailureCount:   2,
		LastError:      "boom",
		FirstSeenAt:    "2026-09-15T10:00:00Z",
	}})
	if diags.HasError() {
		t.Fatal(diags)
	}
	if len(entries) != 1 {
		t.Fatalf("entries: %+v", entries)
	}
	e := entries[0]
	if e.ID.ValueString() != "modrepo-1" || e.Repository.ValueString() != "org/terraform-aws-a" ||
		e.Status.ValueString() != "active" || e.Origin.ValueString() != "new" || e.FailureCount.ValueInt64() != 2 ||
		e.LastError.ValueString() != "boom" || e.DefaultBranch.ValueString() != "main" {
		t.Errorf("entry: %+v", e)
	}
	if len(e.CandidatePaths.Elements()) != 2 || len(e.PreviousPaths.Elements()) != 1 {
		t.Errorf("lists: %v %v", e.CandidatePaths, e.PreviousPaths)
	}
	// Unset timestamps are known empty strings, not null.
	if e.RepoCreatedAt.IsNull() || e.LastCheckedAt.ValueString() != "" {
		t.Errorf("timestamps: %v %v", e.RepoCreatedAt, e.LastCheckedAt)
	}
}

func TestEntriesFromEmpty(t *testing.T) {
	entries, diags := entriesFrom(context.Background(), nil)
	if diags.HasError() || entries == nil || len(entries) != 0 {
		t.Errorf("no repositories should map to an empty, non-nil list: %v %v", entries, diags)
	}
}

func TestSchemaHasNoReservedProviderAttribute(t *testing.T) {
	resp := &datasource.SchemaResponse{}
	NewDataSource().Schema(context.Background(), datasource.SchemaRequest{}, resp)
	if resp.Diagnostics.HasError() {
		t.Fatal(resp.Diagnostics)
	}
	if _, ok := resp.Schema.Attributes["provider"]; ok {
		t.Error(`"provider" is a reserved root attribute name`)
	}
	for _, want := range []string{"rule_id", "status", "repositories"} {
		if _, ok := resp.Schema.Attributes[want]; !ok {
			t.Errorf("missing %s", want)
		}
	}
}
