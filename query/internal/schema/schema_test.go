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
