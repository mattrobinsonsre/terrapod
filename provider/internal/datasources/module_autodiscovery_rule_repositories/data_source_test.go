package module_autodiscovery_rule_repositories

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

func TestEntriesFromMapsEveryField(t *testing.T) {
	ctx := context.Background()
	entries, diags := entriesFrom(ctx, []terrapod.ModuleAutodiscoveryRepository{{
		ID:            "modrepo-1",
		Repository:    "org/terraform-aws-a",
		RepoURL:       "https://github.com/org/terraform-aws-a",
		VCSRepoID:     "R_1",
		DefaultBranch: "main",
		Status:        "active",
		Origin:        "new",
		Candidates: []terrapod.ModuleAutodiscoveryStoredCandidate{
			{Subdirectory: "", Name: "terraform-aws-a", Provider: "aws"},
			{Subdirectory: "modules/x", Name: "x", Provider: "aws"},
		},
		SeenSubdirectories: []string{"", "modules/x"},
		LastSkips: []terrapod.ModuleAutodiscoverySkip{
			{Subdirectory: "", Reason: "already-registered"},
		},
		PreviousPaths: []terrapod.ModuleAutodiscoveryRepositoryPrevious{
			{Path: "old/terraform-aws-a", URL: "https://github.com/old/terraform-aws-a"},
		},
		LastScannedSHA: "abc",
		NextCheckAt:    "2026-09-16T10:00:00Z",
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
	if e.VCSRepoID.ValueString() != "R_1" || e.NextCheckAt.ValueString() != "2026-09-16T10:00:00Z" {
		t.Errorf("entry: %+v", e)
	}
	// The flattening used to discard all of these: the name and provider each
	// candidate would register under, the URL a renamed repository had, and the
	// reason a candidate registered nothing — which is the one that answers
	// "why did this repository register nothing".
	if len(e.Candidates) != 2 || e.Candidates[1].Name.ValueString() != "x" ||
		e.Candidates[1].Provider.ValueString() != "aws" {
		t.Errorf("candidates: %+v", e.Candidates)
	}
	if len(e.LastSkips) != 1 || e.LastSkips[0].Reason.ValueString() != "already-registered" {
		t.Errorf("skips: %+v", e.LastSkips)
	}
	if len(e.PreviousLocations) != 1 ||
		e.PreviousLocations[0].URL.ValueString() != "https://github.com/old/terraform-aws-a" {
		t.Errorf("previous locations: %+v", e.PreviousLocations)
	}
	if len(e.SeenSubdirectories.Elements()) != 2 {
		t.Errorf("seen subdirectories: %v", e.SeenSubdirectories)
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
	repos, ok := resp.Schema.Attributes["repositories"].(schema.ListNestedAttribute)
	if !ok {
		t.Fatal("repositories is not a list of nested objects")
	}
	// Everything the API returns for a repository is reachable from Terraform.
	for _, want := range []string{
		"vcs_repo_id", "candidates", "seen_subdirectories", "last_skips",
		"previous_paths", "previous_locations", "next_check_at",
	} {
		if _, ok := repos.NestedObject.Attributes[want]; !ok {
			t.Errorf("missing repositories.%s", want)
		}
	}
}
