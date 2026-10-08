package provider

import (
	"context"
	"sort"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	rschema "github.com/hashicorp/terraform-plugin-framework/resource/schema"
)

// The three inventory variable resources are the same four fields with
// different parents — `host_vars/<host>`, `group_vars/<group>` and
// `group_vars/all` (#1968). So they are three self-contained packages that
// each hold an identical copy of the masked-value read rule, the
// full-replace update and the plan-time defaults.
//
// Three copies is how one of them gets fixed and the other two do not: a
// per-package test pins what that package does today, and nothing makes a
// change to one of them a change to its siblings. This is the gate that does
// — it asserts the three agree on everything except the parent, so a
// one-sided change fails here and names the attribute that drifted.
//
// It deliberately compares the SCHEMA rather than the source. A behavioural
// difference that does not show up in the schema (a masked read applied in one
// and not another) is pinned by each package's own mutation-checked test; what
// the schema catches is the class nobody notices, where a flag or a default or
// a requiredness quietly stops matching.

// inventoryVarResources maps each variable resource's type name to the
// attribute that names its parent. Everything else must agree.
var inventoryVarResources = map[string]string{
	"terrapod_inventory_host_var":   "host_id",
	"terrapod_inventory_group_var":  "group_id",
	"terrapod_inventory_global_var": "workspace_id",
}

func TestTheThreeInventoryVarResourcesAgreeExceptOnTheirParent(t *testing.T) {
	ctx := context.Background()
	schemas := inventoryVarSchemas(t, ctx)
	if len(schemas) != len(inventoryVarResources) {
		t.Fatalf("expected %d variable resources registered, found %d: %v",
			len(inventoryVarResources), len(schemas), varTypeNames(schemas))
	}

	// The shared attributes, by name, with the parent excluded from each.
	shared := map[string]map[string]rschema.Attribute{}
	for typeName, s := range schemas {
		parent := inventoryVarResources[typeName]
		rest := map[string]rschema.Attribute{}
		for name, a := range s.Attributes {
			if name == parent {
				continue
			}
			rest[name] = a
		}
		shared[typeName] = rest
	}

	reference := "terrapod_inventory_host_var"
	ref := shared[reference]

	for typeName, attrs := range shared {
		if typeName == reference {
			continue
		}
		if got, want := sortedKeys(attrs), sortedKeys(ref); !equal(got, want) {
			t.Errorf("%s has attributes %v, %s has %v — they must agree except on the parent",
				typeName, got, reference, want)
			continue
		}
		for name, a := range attrs {
			r := ref[name]
			if a.IsRequired() != r.IsRequired() || a.IsOptional() != r.IsOptional() ||
				a.IsComputed() != r.IsComputed() || a.IsSensitive() != r.IsSensitive() {
				t.Errorf("%s.%s is required=%t optional=%t computed=%t sensitive=%t "+
					"but %s.%s is required=%t optional=%t computed=%t sensitive=%t",
					typeName, name, a.IsRequired(), a.IsOptional(), a.IsComputed(), a.IsSensitive(),
					reference, name, r.IsRequired(), r.IsOptional(), r.IsComputed(), r.IsSensitive())
			}
			if a.GetType().String() != r.GetType().String() {
				t.Errorf("%s.%s is %s but %s.%s is %s",
					typeName, name, a.GetType().String(),
					reference, name, r.GetType().String())
			}
		}
	}
}

// Each parent attribute must force a replacement, because a variable belongs
// to exactly one host, group or workspace. Without it a reparent would be a
// PATCH to a route that cannot accept one.
func TestEveryInventoryVarParentForcesReplacement(t *testing.T) {
	ctx := context.Background()
	for typeName, s := range inventoryVarSchemas(t, ctx) {
		parent := inventoryVarResources[typeName]
		a, ok := s.Attributes[parent].(rschema.StringAttribute)
		if !ok {
			t.Errorf("%s.%s is not a StringAttribute", typeName, parent)
			continue
		}
		if !a.IsRequired() {
			t.Errorf("%s.%s must be required", typeName, parent)
		}
		replaces := false
		for _, pm := range a.PlanModifiers {
			if strings.Contains(pm.Description(ctx), "destroy and recreate") {
				replaces = true
			}
		}
		if !replaces {
			t.Errorf("%s.%s must force a replacement", typeName, parent)
		}
	}
}

// `key` is renameable on all three, deliberately: the SDK's update request
// carries it as a pointer and the server renames in place, so making a
// practitioner delete and recreate a variable to fix a typo would be work with
// nothing behind it.
func TestKeyIsRenameableOnAllThree(t *testing.T) {
	ctx := context.Background()
	for typeName, s := range inventoryVarSchemas(t, ctx) {
		a, ok := s.Attributes["key"].(rschema.StringAttribute)
		if !ok {
			t.Errorf("%s.key is not a StringAttribute", typeName)
			continue
		}
		for _, pm := range a.PlanModifiers {
			if strings.Contains(pm.Description(ctx), "destroy and recreate") {
				t.Errorf("%s.key must NOT force a replacement: the server renames in place", typeName)
			}
		}
	}
}

// inventoryVarSchemas reads the three variable resources' schemas off the
// PROVIDER's own registration, so a resource that is written but never
// registered — or registered under the wrong type name — fails here rather
// than passing on the strength of a package-local test.
func inventoryVarSchemas(t *testing.T, ctx context.Context) map[string]rschema.Schema {
	t.Helper()
	out := map[string]rschema.Schema{}
	for _, newResource := range New("test")().Resources(ctx) {
		r := newResource()
		var md resource.MetadataResponse
		r.Metadata(ctx, resource.MetadataRequest{ProviderTypeName: providerTypeName}, &md)
		if _, want := inventoryVarResources[md.TypeName]; !want {
			continue
		}
		var sr resource.SchemaResponse
		r.Schema(ctx, resource.SchemaRequest{}, &sr)
		if sr.Diagnostics.HasError() {
			t.Fatalf("%s schema: %v", md.TypeName, sr.Diagnostics)
		}
		out[md.TypeName] = sr.Schema
	}
	return out
}

func sortedKeys(m map[string]rschema.Attribute) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func varTypeNames(m map[string]rschema.Schema) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func equal(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
