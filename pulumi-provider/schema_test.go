package terrapod

import (
	"context"
	"encoding/json"
	"os"
	"testing"

	"github.com/mattrobinsonsre/terrapod/provider/tfprovider"
)

// theSchema is the committed, generated Pulumi Package Schema.
//
// Read from disk rather than from the embedded copy on purpose: the embed lives
// in the plugin's `main`, and what these tests are about is whether the
// COMMITTED artifact is correct. A test reading the embed would pass on a stale
// commit of a correct build.
func theSchema(t *testing.T) map[string]any {
	t.Helper()
	raw, err := os.ReadFile("cmd/pulumi-resource-terrapod/schema.json")
	if err != nil {
		t.Fatalf("the generated schema is missing -- run `go run ./cmd/pulumi-tfgen-terrapod schema "+
			"--out cmd/pulumi-resource-terrapod`: %v", err)
	}
	var s map[string]any
	if err := json.Unmarshal(raw, &s); err != nil {
		t.Fatalf("the generated schema is not valid JSON: %v", err)
	}
	return s
}

// TestEveryTerraformResourceIsMapped is the gate that matters, and it is
// DERIVED rather than a number typed into a test.
//
// The whole argument for bridging (#1987) is that a resource added to the
// Terraform provider appears in Pulumi with no edit here. Nothing enforced
// that: the token strategy maps what it is given, so a resource it failed to
// map would simply be absent, and a Pulumi user would find the resource missing
// with every gate green. Counting the Terraform provider's own declarations and
// comparing is what turns the bridging argument into something checked.
func TestEveryTerraformResourceIsMapped(t *testing.T) {
	ctx := context.Background()
	p := tfprovider.New("0.0.0-test")()

	res := p.Resources(ctx)
	ds := p.DataSources(ctx)

	// A floor, so a changed framework API that returned nothing would fail here
	// rather than make every assertion below pass by comparing 0 with 0.
	if len(res) < 20 || len(ds) < 5 {
		t.Fatalf("only %d resources and %d data sources read from the Terraform provider; "+
			"the accessors have probably changed rather than the provider having shrunk",
			len(res), len(ds))
	}

	s := theSchema(t)
	schemaResources, _ := s["resources"].(map[string]any)
	if got, want := len(schemaResources), len(res); got != want {
		t.Errorf("the Pulumi schema has %d resources and the Terraform provider declares %d.\n"+
			"A resource added to the Terraform provider must appear here automatically -- that is "+
			"the entire case for bridging. Regenerate the schema; if the count still differs, the "+
			"token strategy failed to map something and a Pulumi user cannot reach it.", got, want)
	}
}

// TestAllFourLanguagesAreConfigured guards a failure that is silent rather than
// loud: with no `Golang`/`CSharp` blocks the generator emits a `language`
// section holding only `nodejs` and `python` and exits 0, so two of the four
// SDKs simply cannot be generated. Measured -- that is what it did before those
// blocks were added.
func TestAllFourLanguagesAreConfigured(t *testing.T) {
	s := theSchema(t)
	lang, _ := s["language"].(map[string]any)
	for _, want := range []string{"nodejs", "python", "go", "csharp"} {
		if _, ok := lang[want]; !ok {
			t.Errorf("the schema has no %q language block, so that SDK cannot be generated; "+
				"got %v", want, keys(lang))
		}
	}
}

// TestThePackageNamesAreTheOnesTerrapodServes pins the operator-facing names.
//
// Terrapod serves these packages itself (decided 2026-10-28), so the name is
// what an operator types against their own instance's package cache. A silent
// rename would break every program that installed the old one, and the default
// the bridge would otherwise pick is not these.
func TestThePackageNamesAreTheOnesTerrapodServes(t *testing.T) {
	s := theSchema(t)
	lang, _ := s["language"].(map[string]any)
	for _, c := range []struct{ lang, key, want string }{
		{"nodejs", "packageName", "@terrapod/pulumi"},
		{"python", "packageName", "terrapod-pulumi"},
		{"csharp", "rootNamespace", "Terrapod"},
		{"go", "importBasePath", "github.com/mattrobinsonsre/terrapod/pulumi-provider/sdk/go/terrapod"},
	} {
		block, _ := lang[c.lang].(map[string]any)
		if got, _ := block[c.key].(string); got != c.want {
			t.Errorf("%s %s = %q, want %q", c.lang, c.key, got, c.want)
		}
	}
}

// TestTheLicenceIsDeclared exists because it was nearly shipped empty.
//
// `TFProviderLicense` describes the UPSTREAM provider; `License` is "the licence
// the resulting package has", and its default is none. Setting only the first
// leaves all four published SDKs declaring no licence for an MPL-2.0 project,
// and the generated schema is the only place that is visible.
func TestTheLicenceIsDeclared(t *testing.T) {
	s := theSchema(t)
	if got, _ := s["license"].(string); got != "MPL-2.0" {
		t.Errorf("schema license = %q, want %q -- `License` is a different field from "+
			"`TFProviderLicense` and defaults to none", got, "MPL-2.0")
	}
}

// TestTheInventoryResourcesAreReachable names what the issue exists for.
//
// A Pulumi workspace could declare no inventory at all; these seven tokens are
// the feature. They are asserted by name rather than by count so that a
// regression names the structure a program lost.
func TestTheInventoryResourcesAreReachable(t *testing.T) {
	s := theSchema(t)
	res, _ := s["resources"].(map[string]any)
	for _, tok := range []string{
		"terrapod:index/inventoryHost:InventoryHost",
		"terrapod:index/inventoryGroup:InventoryGroup",
		"terrapod:index/inventoryHostGroup:InventoryHostGroup",
		"terrapod:index/inventoryGroupChild:InventoryGroupChild",
		"terrapod:index/inventoryHostVar:InventoryHostVar",
		"terrapod:index/inventoryGroupVar:InventoryGroupVar",
		"terrapod:index/inventoryGlobalVar:InventoryGlobalVar",
	} {
		if _, ok := res[tok]; !ok {
			t.Errorf("inventory resource %q is absent, so a Pulumi program cannot declare it", tok)
		}
	}
	fns, _ := s["functions"].(map[string]any)
	if _, ok := fns["terrapod:index/getInventoryResolved:getInventoryResolved"]; !ok {
		t.Error("the resolved-inventory data source is absent, so a program cannot read back " +
			"the inventory it declared")
	}
}

func keys(m map[string]any) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
