package terrapod

import (
	"context"
	"encoding/json"
	"os"
	"strings"
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
		{"python", "packageName", "terrapod_pulumi"},
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

// TestNoFunctionsOutputHelperCollidesWithAGeneratedType is a regression test
// named after the failure mode, and it is DERIVED from the schema's own
// function list rather than naming the one case that bit (#2284).
//
// Pulumi emits an output-form invoke helper called `<Function>Output` for every
// function, and it derives a nested type's token from the function token plus
// the property name. So a data source with a property called `outputs` produces
// a type whose Go name is exactly the name already taken by that function's own
// helper. In Go that is a redeclaration and the generated SDK does not compile
// -- eleven errors from one name, because it cascades into `...OutputArgs` and
// its `ElementType`.
//
// `terrapod_catalog_item_interface` did exactly that, with `outputs` renamed to
// `moduleOutputs` in `Provider()` to clear it. This asserts the PROPERTY rather
// than that one rename, so the next data source to grow an `outputs` attribute
// fails here instead of reaching whoever generates an SDK first.
//
// The CI job that generates and builds the Go SDK is the authoritative gate --
// it catches anything that stops a build, not one shape. This is the fast,
// dependency-free half: it runs with the other Go tests and says, in words, what
// the rule is.
func TestNoFunctionsOutputHelperCollidesWithAGeneratedType(t *testing.T) {
	s := theSchema(t)

	fns, ok := s["functions"].(map[string]any)
	if !ok || len(fns) == 0 {
		t.Fatal("the schema declares no functions -- this test would then assert nothing")
	}
	types, ok := s["types"].(map[string]any)
	if !ok || len(types) == 0 {
		t.Fatal("the schema declares no types -- this test would then assert nothing")
	}

	// `terrapod:index/getX:getX` -> `GetX`, which is how the Go generator names
	// it; `<that>Output` is the name it reserves for the output-form helper.
	goName := func(token string) string {
		leaf := token
		if i := strings.LastIndex(token, ":"); i >= 0 {
			leaf = token[i+1:]
		}
		if leaf == "" {
			return ""
		}
		return strings.ToUpper(leaf[:1]) + leaf[1:]
	}

	reserved := make(map[string]string, len(fns))
	for fn := range fns {
		if n := goName(fn); n != "" {
			reserved[n+"Output"] = fn
		}
	}

	for typ := range types {
		n := goName(typ)
		if fn, taken := reserved[n]; taken {
			t.Errorf(
				"generated type %q has the Go name %q, which Pulumi reserves for function %q's "+
					"output-form helper -- the Go SDK will not compile. Rename the offending "+
					"PROPERTY via `Fields` in Provider(): that renames the type as a consequence. "+
					"Note `SchemaInfo.NestedType` is silently ignored here, and renaming the data "+
					"source token cannot help, because the type name is derived from the token AND "+
					"the property.",
				typ, n, fn,
			)
		}
	}

	if len(reserved) < 10 {
		t.Errorf("only %d function helper names were derived, which is too few to be the real "+
			"surface -- has the schema or the token shape changed?", len(reserved))
	}
}

// TestEachLanguagesPackageNameIsUsableInThatLanguage is the general form of a
// defect that shipped: the Python SDK installed and could never be imported.
//
// `PythonInfo.PackageName` is one field with two uses -- the generator makes it
// the distribution name AND the module directory. A hyphen is legal in the first
// and illegal in the second, so `terrapod-pulumi` generated `terrapod-pulumi/`:
// `import terrapod_pulumi` is ModuleNotFoundError and `import terrapod-pulumi`
// is a SyntaxError. Nothing caught it, because the name was asserted as a STRING
// and a string cannot be wrong (#2267).
//
// So this asserts the PROPERTY instead. Python is the one with a real constraint
// worth encoding -- a module name must be an identifier -- and the other three
// are checked for the shapes that would be obviously wrong in their own
// ecosystem rather than guessed at.
func TestEachLanguagesPackageNameIsUsableInThatLanguage(t *testing.T) {
	s := theSchema(t)

	lang, ok := s["language"].(map[string]any)
	if !ok || len(lang) == 0 {
		t.Fatal("the schema declares no language blocks -- this test would then assert nothing")
	}

	// A Python module name is an identifier: letters, digits and underscores,
	// not starting with a digit. Notably NOT hyphens, which is the whole point.
	py, ok := lang["python"].(map[string]any)
	if !ok {
		t.Fatal("no python language block")
	}
	name, _ := py["packageName"].(string)
	if name == "" {
		t.Fatal("the python block declares no packageName")
	}
	for i, r := range name {
		valid := r == '_' ||
			(r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') ||
			(r >= '0' && r <= '9' && i > 0)
		if !valid {
			t.Errorf(
				"python packageName %q is not a valid Python module name (%q at %d). The "+
					"generator uses this field as the module DIRECTORY as well as the "+
					"distribution name, so the SDK would install and never import. Use "+
					"underscores; pip normalises them to hyphens for the distribution anyway.",
				name, r, i,
			)
			break
		}
	}

	// The others, each against what its own ecosystem forbids rather than a
	// guess: an npm name is lowercase, and a C# root namespace is dotted
	// identifiers.
	if js, ok := lang["nodejs"].(map[string]any); ok {
		if n, _ := js["packageName"].(string); n != strings.ToLower(n) {
			t.Errorf("nodejs packageName %q is not lowercase, which npm requires", n)
		}
	}
	if cs, ok := lang["csharp"].(map[string]any); ok {
		if n, _ := cs["rootNamespace"].(string); strings.ContainsAny(n, "- ") {
			t.Errorf("csharp rootNamespace %q contains a character C# does not allow in an identifier", n)
		}
	}
}
