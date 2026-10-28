package terrapod

import (
	"encoding/json"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"strings"
	"testing"
)

// The Pulumi Package Schema must carry a version, and NOTHING ELSE CAN SUPPLY IT.
//
// `pulumi-tfgen` clears the version when it emits `schema.json`, deliberately and
// with its own comment saying why ("so that the spec is stable if the version is
// e.g. derived from the current Git commit hash"), and the plugin-framework
// provider server then returns `ProviderMetadata.PackageSchema` verbatim -- it
// parses `info.Version` for its own use and never injects it, unlike the SDKv2
// path whose `tfbridge.NewProvider` takes the version as an explicit argument.
//
// With no version, `pulumi package add` refuses before doing any
// language-specific work:
//
//	error: #/version: version must be provided when package supports packing
//
// which is the provider's ONLY documented consumption route, since the local-SDK
// path is what exists instead of publishing to npm, PyPI and NuGet. It had been
// broken since the provider shipped (#1987) with every gate green, because
// `schema_test.go` reads the committed schema and has no opinion about `version`
// -- a derived gate's blind spot is decided by its derivation.
//
// Verified by running it: with a version, `pulumi package add` generates the
// SDK, a `terrapod.InventoryHost` type-checks against it in TypeScript, and the
// control (a misspelled attribute) is refused.

func TestTheCommittedSchemaHasNoVersion(t *testing.T) {
	// Asserted rather than assumed, and this is not a defect: tfgen omits it on
	// purpose, which is exactly why the plugin has to add it. If a future bridge
	// starts emitting one, this fails and tells us the injection is now
	// redundant -- better than silently setting a field twice.
	raw, err := os.ReadFile("cmd/pulumi-resource-terrapod/schema.json")
	if err != nil {
		t.Fatalf("the generated schema is missing: %v", err)
	}
	var spec map[string]any
	if err := json.Unmarshal(raw, &spec); err != nil {
		t.Fatalf("the committed schema is not valid JSON: %v", err)
	}
	if v, ok := spec["version"]; ok {
		t.Errorf("the committed schema now carries version %v. tfgen used to omit it "+
			"deliberately; if that changed upstream, `SchemaWithVersion` is redundant "+
			"and the plugin should stop injecting rather than overwrite", v)
	}
}

func TestSchemaWithVersionSetsItAndPreservesEverythingElse(t *testing.T) {
	raw, err := os.ReadFile("cmd/pulumi-resource-terrapod/schema.json")
	if err != nil {
		t.Fatalf("the generated schema is missing: %v", err)
	}

	out, err := SchemaWithVersion(raw, "2.0.0")
	if err != nil {
		t.Fatalf("SchemaWithVersion: %v", err)
	}

	var before, after map[string]any
	if err := json.Unmarshal(raw, &before); err != nil {
		t.Fatalf("unmarshalling the input: %v", err)
	}
	if err := json.Unmarshal(out, &after); err != nil {
		t.Fatalf("the result is not valid JSON: %v", err)
	}

	if got := after["version"]; got != "2.0.0" {
		t.Errorf("version = %v, want 2.0.0", got)
	}

	// EVERY other key survives, and by comparison rather than by spot-check:
	// the round-trip is the risk being guarded, because a typed re-marshal would
	// drop any key the struct of the day does not model. `version` is the only
	// permitted difference.
	delete(after, "version")
	if len(before) != len(after) {
		t.Errorf("the key set changed: %d keys in, %d out (version excluded)", len(before), len(after))
	}
	for k := range before {
		if _, ok := after[k]; !ok {
			t.Errorf("key %q was dropped by the round-trip", k)
		}
	}

	// And the load-bearing contents are intact, not merely present.
	res, _ := after["resources"].(map[string]any)
	if len(res) == 0 {
		t.Error("the result declares no resources")
	}
	if n, _ := after["name"].(string); n != Namespace {
		t.Errorf("name = %q, want %q", n, Namespace)
	}
	lang, _ := after["language"].(map[string]any)
	for _, l := range []string{"nodejs", "python", "go", "csharp"} {
		if _, ok := lang[l]; !ok {
			t.Errorf("the %q language block did not survive the round-trip", l)
		}
	}
}

func TestSchemaWithVersionRefusesWhatItCannotVouchFor(t *testing.T) {
	// An empty version is the case that matters: it is what a plugin built
	// without ldflags would have, and serving a schema with `"version": ""` is
	// the same failure with a field present, which is harder to diagnose than
	// a refusal at startup.
	if _, err := SchemaWithVersion([]byte(`{"name":"terrapod"}`), ""); err == nil {
		t.Error("an empty version was accepted; the plugin would serve an unusable schema")
	}
	if _, err := SchemaWithVersion([]byte(`not json`), "2.0.0"); err == nil {
		t.Error("invalid JSON was accepted")
	}
}

// TestThePluginActuallyInjectsTheVersion is the call-site gate.
//
// The function existing and working is not the same as `main` using it: a
// correct helper nothing calls is this repository's most repeated defect, and
// here it would restore the whole bug while every test above stayed green.
// There is no seam to drive `main()` through, so the source is the subject --
// the same shape as the positional gates elsewhere in the tree.
func TestThePluginActuallyInjectsTheVersion(t *testing.T) {
	const path = "cmd/pulumi-resource-terrapod/main.go"
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, path, nil, 0)
	if err != nil {
		t.Fatalf("parsing %s: %v", path, err)
	}

	var calls, metadataArgs int
	ast.Inspect(file, func(n ast.Node) bool {
		call, ok := n.(*ast.CallExpr)
		if !ok {
			return true
		}
		if sel, ok := call.Fun.(*ast.SelectorExpr); ok && sel.Sel.Name == "SchemaWithVersion" {
			calls++
		}
		// The result must be what is HANDED TO THE BRIDGE. Calling the helper and
		// passing the raw `schema` anyway is the half-wiring this catches, and it
		// is invisible to every behavioural test.
		if lit, ok := call.Fun.(*ast.SelectorExpr); ok && lit.Sel.Name == "Main" {
			for _, arg := range call.Args {
				composite, ok := arg.(*ast.CompositeLit)
				if !ok {
					continue
				}
				for _, elt := range composite.Elts {
					kv, ok := elt.(*ast.KeyValueExpr)
					if !ok {
						continue
					}
					if key, ok := kv.Key.(*ast.Ident); ok && key.Name == "PackageSchema" {
						metadataArgs++
						ident, ok := kv.Value.(*ast.Ident)
						if !ok || ident.Name == "schema" {
							t.Errorf("PackageSchema is passed %s -- the raw embedded bytes "+
								"carry no version, so the plugin serves a schema no SDK can "+
								"be generated from", render(kv.Value))
						}
					}
				}
			}
		}
		return true
	})

	if calls == 0 {
		t.Error("main.go never calls SchemaWithVersion, so the embedded schema is served unversioned")
	}
	if metadataArgs == 0 {
		t.Error("no PackageSchema argument found in the bridge Main call -- teach this gate " +
			"the new shape rather than deleting it")
	}
}

func render(e ast.Expr) string {
	switch v := e.(type) {
	case *ast.Ident:
		return v.Name
	default:
		return strings.TrimSpace("a non-identifier expression")
	}
}
