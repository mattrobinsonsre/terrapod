package schema

import (
	"os"
	"path/filepath"
	"testing"
)

func loadFixture(t *testing.T) *Schema {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", "sample-schema.json"))
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	s, err := Parse(b)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	return s
}

func find(cat []DataSource, name string) (DataSource, bool) {
	for _, d := range cat {
		if d.Name == name {
			return d, true
		}
	}
	return DataSource{}, false
}

func TestCatalogueCoversEveryProviderAndDataSource(t *testing.T) {
	cat := loadFixture(t).Catalogue()
	if len(cat) != 4 {
		t.Fatalf("expected 4 data sources across both providers, got %d", len(cat))
	}
	// Sorted by provider then name: aws_* before google_*.
	if cat[0].Provider != "registry.opentofu.org/hashicorp/aws" {
		t.Errorf("catalogue not sorted by provider: first is %q", cat[0].Provider)
	}
	if _, ok := find(cat, "google_compute_instance"); !ok {
		t.Error("second provider's data source missing from catalogue")
	}
}

func TestClassifyFilterTagsAndListSignals(t *testing.T) {
	cat := loadFixture(t).Catalogue()

	vpcs, ok := find(cat, "aws_vpcs")
	if !ok {
		t.Fatal("aws_vpcs missing")
	}
	if !vpcs.HasFilter || !vpcs.HasTags || !vpcs.ReturnsList {
		t.Errorf("aws_vpcs should have filter+tags+list, got %+v", vpcs)
	}
	if !vpcs.Queryable() {
		t.Error("aws_vpcs should be queryable")
	}
	// Settable inputs are the filter block + tags; the computed `ids` output is
	// not an input.
	assertEqualSet(t, "aws_vpcs.Inputs", vpcs.Inputs, []string{"filter", "tags"})
	if len(vpcs.RequiredInputs) != 0 {
		t.Errorf("aws_vpcs has no required inputs, got %v", vpcs.RequiredInputs)
	}
}

func TestComputedOnlyTagsIsNotAnInput(t *testing.T) {
	// aws_vpc's `tags` is computed-only → not HasTags, not an input. But it has
	// a filter block, so it's still queryable.
	vpc, ok := find(loadFixture(t).Catalogue(), "aws_vpc")
	if !ok {
		t.Fatal("aws_vpc missing")
	}
	if vpc.HasTags {
		t.Error("aws_vpc tags is computed-only; HasTags should be false")
	}
	if !vpc.HasFilter || !vpc.Queryable() {
		t.Error("aws_vpc has a filter block; should be queryable")
	}
	for _, in := range vpc.Inputs {
		if in == "tags" {
			t.Error("computed-only tags leaked into Inputs")
		}
	}
}

func TestNonDiscoveryDataSourceExcludedFromStrongSubset(t *testing.T) {
	s := loadFixture(t)

	// aws_caller_identity has no filter/tags/ids — appears in the full
	// catalogue but not in the strong queryable subset.
	if ci, ok := find(s.Catalogue(), "aws_caller_identity"); !ok {
		t.Error("aws_caller_identity should be in the full catalogue")
	} else if ci.Queryable() {
		t.Error("aws_caller_identity has no discovery signal; should not be queryable")
	}

	// google_compute_instance only accepts an optional name/project/zone — a
	// soft candidate: catalogued with inputs, but not a strong signal.
	gci, ok := find(s.Catalogue(), "google_compute_instance")
	if !ok {
		t.Fatal("google_compute_instance missing")
	}
	if gci.Queryable() {
		t.Error("google_compute_instance has no filter/tags/list; not a strong candidate")
	}
	assertEqualSet(t, "google_compute_instance.Inputs", gci.Inputs, []string{"name", "project", "zone"})

	// The strong subset is exactly {aws_vpc, aws_vpcs}.
	q := s.QueryableDataSources()
	if len(q) != 2 {
		t.Fatalf("expected 2 strong candidates, got %d: %+v", len(q), q)
	}
}

func TestImportableRequiresIdsListNotJustQueryable(t *testing.T) {
	s := loadFixture(t)

	// aws_vpc is queryable (it has a filter block) but exposes no computed `ids`
	// list — the same shape as aws_eips (a filter but allocation_ids, not ids).
	// The deterministic import path can't derive ids from it, so it must NOT be
	// importable even though it is queryable.
	vpc, _ := find(s.Catalogue(), "aws_vpc")
	if !vpc.Queryable() {
		t.Fatal("precondition: aws_vpc should be queryable (has a filter)")
	}
	if vpc.Importable() {
		t.Error("aws_vpc has no computed `ids`; must not be importable")
	}

	// aws_vpcs has a computed `ids` list → importable.
	vpcs, _ := find(s.Catalogue(), "aws_vpcs")
	if !vpcs.Importable() {
		t.Error("aws_vpcs exposes a computed `ids` list; should be importable")
	}

	// The importable surface is exactly {aws_vpcs} — a strict subset of the
	// queryable surface {aws_vpc, aws_vpcs}, proving a source can be queryable
	// yet excluded from onboarding because import can't consume it.
	imp := s.ImportableDataSources()
	if len(imp) != 1 || imp[0].Name != "aws_vpcs" {
		t.Fatalf("expected importable surface = [aws_vpcs], got %+v", imp)
	}
}

func TestIDListAttrGeneralisesBeyondIds(t *testing.T) {
	// aws_eips exposes `allocation_ids` (not `ids`) plus a synthetic scalar `id`
	// (region) and a `public_ips` list. The single `*_ids` list is the id list;
	// the resource aws_eip exists, so it is importable.
	doc := `{"format_version":"1.0","provider_schemas":{"registry.opentofu.org/hashicorp/aws":{
	  "resource_schemas":{"aws_eip":{"version":0,"block":{"attributes":{"id":{"type":"string","computed":true}}}}},
	  "data_source_schemas":{"aws_eips":{"version":0,"block":{
	    "attributes":{
	      "id":{"type":"string","computed":true},
	      "allocation_ids":{"type":["list","string"],"computed":true},
	      "public_ips":{"type":["list","string"],"computed":true}},
	    "block_types":{"filter":{"nesting_mode":"set","block":{}}}}}}}}}`
	s, err := Parse([]byte(doc))
	if err != nil {
		t.Fatal(err)
	}
	eips, ok := find(s.Catalogue(), "aws_eips")
	if !ok {
		t.Fatal("aws_eips missing")
	}
	if eips.IDListAttr != "allocation_ids" {
		t.Errorf("IDListAttr = %q, want allocation_ids", eips.IDListAttr)
	}
	if eips.ResourceType != "aws_eip" {
		t.Errorf("ResourceType = %q, want aws_eip", eips.ResourceType)
	}
	if !eips.Importable() {
		t.Error("aws_eips (allocation_ids + aws_eip resource exists) should be importable")
	}
}

func TestImportableRequiresTheTargetResourceToExist(t *testing.T) {
	// aws_availability_zones has a computed `ids` list but singularises to
	// aws_availability_zone, which is NOT a managed resource — so it must be
	// excluded even though it has an id list (the authoritative existence check).
	doc := `{"format_version":"1.0","provider_schemas":{"registry.opentofu.org/hashicorp/aws":{
	  "resource_schemas":{"aws_vpc":{"version":0,"block":{"attributes":{"id":{"type":"string","computed":true}}}}},
	  "data_source_schemas":{"aws_availability_zones":{"version":0,"block":{
	    "attributes":{"ids":{"type":["list","string"],"computed":true}}}}}}}}`
	s, err := Parse([]byte(doc))
	if err != nil {
		t.Fatal(err)
	}
	az, _ := find(s.Catalogue(), "aws_availability_zones")
	if az.IDListAttr != "ids" {
		t.Errorf("precondition: IDListAttr = %q, want ids", az.IDListAttr)
	}
	if az.ResourceType != "" {
		t.Errorf("aws_availability_zone is not a managed resource; ResourceType should be empty, got %q", az.ResourceType)
	}
	if az.Importable() {
		t.Error("no matching managed resource → must not be importable")
	}
	if len(s.ImportableDataSources()) != 0 {
		t.Error("importable surface should be empty")
	}
}

func assertEqualSet(t *testing.T, label string, got, want []string) {
	t.Helper()
	if len(got) != len(want) {
		t.Errorf("%s = %v, want %v", label, got, want)
		return
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("%s = %v, want %v", label, got, want)
			return
		}
	}
}
