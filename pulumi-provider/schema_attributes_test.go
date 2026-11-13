package terrapod

import (
	"context"
	"sort"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/providerserver"
	tfprotov6 "github.com/hashicorp/terraform-plugin-go/tfprotov6"
	"github.com/hashicorp/terraform-plugin-go/tftypes"
	"github.com/mattrobinsonsre/terrapod/provider/tfprovider"
)

// The committed schema must carry every ATTRIBUTE, not merely every resource.
//
// `TestEveryTerraformResourceIsMapped` derives its subject at the resource
// level, and a derived gate's derivation decides its blind spot: an attribute
// added to the Terraform provider drifted out of the committed schema with
// every gate green, and `ci.yml` said the opposite in as many words -- "these
// tests read the COMMITTED schema.json, so they fail when it is stale against
// the Terraform provider, which is the whole contract." They did not.
//
// Measured when this was written: the committed schema was missing TEN
// workspace attributes -- `engine_version`, `execution_backend`,
// `execution_backend_constraints`, `hard_limit`, `dynamic_inventory_enabled`,
// `inventory_scripts_enabled`, `ansible_host_key_checking`, `self_contained`,
// `update_block_version`, `update_block_reason` -- plus `default_run_kind`,
// which is what led here. A Pulumi practitioner could not set any of them, and
// nothing said so.
//
// Comparing SETS rather than counts, because a rename is a removal and an
// addition at once and leaves a count where it was.
func TestEveryTerraformAttributeReachesTheCommittedSchema(t *testing.T) {
	ctx := context.Background()
	committed := theSchema(t)
	resources, _ := committed["resources"].(map[string]any)
	if len(resources) < 20 {
		t.Fatalf("only %d resources in the committed schema; this gate is comparing "+
			"against nothing", len(resources))
	}

	tf := terraformResourceAttributes(ctx, t)
	if len(tf) < 20 {
		t.Fatalf("only %d resources read from the Terraform provider; the accessors have "+
			"probably changed rather than the provider having shrunk", len(tf))
	}

	var checked int
	for tfName, attrs := range tf {
		// The schema's token for `terrapod_workspace` is
		// `terrapod:index/workspace:Workspace`; find it by its tail rather than
		// by rebuilding the token, which is the token strategy's business.
		token := tokenFor(resources, tfName)
		if token == "" {
			// Absent entirely is the sibling gate's finding, not this one's --
			// reporting it here too would give one defect two voices.
			continue
		}
		entry, _ := resources[token].(map[string]any)
		props, _ := entry["inputProperties"].(map[string]any)

		var missing []string
		for _, a := range attrs {
			if _, ok := props[pulumiName(a)]; !ok {
				missing = append(missing, a+" ("+pulumiName(a)+")")
			}
		}
		checked += len(attrs)
		if len(missing) > 0 {
			sort.Strings(missing)
			t.Errorf("%s is missing %d attribute(s) from the committed Pulumi schema: %s.\n"+
				"Regenerate it with `go run ./cmd/pulumi-tfgen-terrapod schema --out "+
				"cmd/pulumi-resource-terrapod`. Until then a Pulumi practitioner cannot "+
				"set them, and no other gate says so.",
				tfName, len(missing), strings.Join(missing, ", "))
		}
	}

	// A floor on the ATTRIBUTES, not just the resources: the loop above skips a
	// resource it cannot find a token for, so a broken `tokenFor` would check
	// nothing while reporting success.
	if checked < 200 {
		t.Fatalf("only %d attributes compared; the derivation is not finding the "+
			"provider's schemas", checked)
	}
	t.Logf("compared %d attributes across %d resources", checked, len(tf))
}

// terraformResourceAttributes reads each resource's top-level attribute names
// straight off the Terraform provider, through the protocol server -- the same
// path `terraform` itself uses, so there is no second opinion about what the
// provider declares.
func terraformResourceAttributes(ctx context.Context, t *testing.T) map[string][]string {
	t.Helper()

	server := providerserver.NewProtocol6(tfprovider.New("0.0.0-test")())()
	resp, err := server.GetProviderSchema(ctx, &tfprotov6.GetProviderSchemaRequest{})
	if err != nil {
		t.Fatalf("GetProviderSchema: %v", err)
	}

	out := make(map[string][]string, len(resp.ResourceSchemas))
	for name, s := range resp.ResourceSchemas {
		if s == nil || s.Block == nil {
			continue
		}
		attrs := make([]string, 0, len(s.Block.Attributes))
		for _, a := range s.Block.Attributes {
			// `id` is Pulumi's own resource identity and is not an input
			// property, so it is not drift when it is absent.
			if a.Name == "id" || a.Computed && !a.Optional && !a.Required {
				continue
			}
			attrs = append(attrs, a.Name)
		}
		out[name] = attrs
	}
	return out
}

// tokenFor finds the schema token whose type name matches a Terraform resource
// name -- `terrapod_workspace` -> `terrapod:index/workspace:Workspace`.
func tokenFor(resources map[string]any, tfName string) string {
	want := strings.ReplaceAll(strings.TrimPrefix(tfName, "terrapod_"), "_", "")
	for token := range resources {
		i := strings.LastIndex(token, ":")
		if i < 0 {
			continue
		}
		if strings.EqualFold(token[i+1:], want) {
			return token
		}
	}
	return ""
}

// pulumiName is tfbridge's snake_case -> camelCase mapping.
//
// Reimplemented here rather than imported because the bridge does not export
// it, and asserted against the real schema by
// TestTheNameMappingAgreesWithTheGeneratedSchema below -- so a special case it
// does not know about fails there, loudly, rather than quietly making this
// gate's comparison wrong.
func pulumiName(snake string) string {
	parts := strings.Split(snake, "_")
	for i := 1; i < len(parts); i++ {
		if parts[i] == "" {
			continue
		}
		parts[i] = strings.ToUpper(parts[i][:1]) + parts[i][1:]
	}
	return strings.Join(parts, "")
}

// TestTheNameMappingAgreesWithTheGeneratedSchema keeps the helper above honest.
//
// It is the one piece of the gate that is a reimplementation rather than a
// reading, so it is the one that can be silently wrong -- and wrong in the
// direction that matters: a name this helper mangles reads as a missing
// attribute, which would send someone regenerating a schema that is already
// correct. So every property the generator actually emitted must be reachable
// by mapping some Terraform attribute name, and vice versa, for one resource
// rich enough to exercise it.
func TestTheNameMappingAgreesWithTheGeneratedSchema(t *testing.T) {
	ctx := context.Background()
	committed := theSchema(t)
	resources, _ := committed["resources"].(map[string]any)

	token := tokenFor(resources, "terrapod_workspace")
	if token == "" {
		t.Fatal("terrapod_workspace is not in the committed schema")
	}
	entry, _ := resources[token].(map[string]any)
	props, _ := entry["inputProperties"].(map[string]any)

	tf := terraformResourceAttributes(ctx, t)
	attrs := tf["terrapod_workspace"]
	if len(attrs) < 40 {
		t.Fatalf("only %d attributes on terrapod_workspace; the derivation is wrong", len(attrs))
	}

	mapped := make(map[string]string, len(attrs))
	for _, a := range attrs {
		mapped[pulumiName(a)] = a
	}

	var unmappable []string
	for p := range props {
		if _, ok := mapped[p]; !ok {
			unmappable = append(unmappable, p)
		}
	}
	if len(unmappable) > 0 {
		sort.Strings(unmappable)
		t.Errorf("the generator emitted %d property name(s) this test's snake->camel mapping "+
			"cannot produce: %s.\nThe mapping is a reimplementation of tfbridge's and has "+
			"met a case it does not know about -- teach `pulumiName`, do NOT loosen the "+
			"gate above, which would then read a correct schema as stale.",
			len(unmappable), strings.Join(unmappable, ", "))
	}
}

// The committed schema must carry every DATA-SOURCE attribute too.
//
// The sibling gate above walks `committed["resources"]`, and the provider's 14
// data sources live under `functions`, which nothing read. So a third of the
// surface had no attribute-level gate at all.
//
// Why this needs more care than the resource half, and why it is worth having:
// a NAIVE comparison of Terraform names against function properties reports
// THREE false positives on a tree that is complete, because the bridge
// transforms names on the way through. Measured while writing this --
//
//	terrapod_architecture_critique.deferred    -> deferreds     (pluralised)
//	terrapod_workspace_cost.unpriced           -> unpriceds     (pluralised)
//	terrapod_catalog_item_interface.outputs    -> moduleOutputs (renamed)
//
// -- and a gate whose every finding is a false positive is one people learn to
// silence. #2555 was raised claiming two of those were MISSING; they were not,
// and the error was matching the Terraform name against the Pulumi schema.
//
// Both transforms are DERIVED rather than ledgered:
//
//   - the rename is read from our OWN bridge configuration, `Provider()`'s
//     `DataSources[tf].Fields[attr].Name`, so a second rename configured there
//     is understood without touching this file;
//   - pluralisation applies only to a LIST or SET attribute, and rather than
//     reimplementing the bridge's English pluraliser -- which would be a second
//     implementation able to be silently wrong -- a list attribute is satisfied
//     by its camelCase name OR a name extending it. A SCALAR is held to the
//     exact camelCase name, so the looser rule buys nothing it should not.
//
// A data source's attributes are almost all computed, so the comparison is
// against `outputs.properties`. Comparing against `inputs` would pass on
// nothing: `getWorkspaceCost` has one input and thirteen outputs.
func TestEveryTerraformDataSourceAttributeReachesTheCommittedSchema(t *testing.T) {
	ctx := context.Background()
	committed := theSchema(t)
	functions, _ := committed["functions"].(map[string]any)
	if len(functions) < 10 {
		t.Fatalf("only %d functions in the committed schema; this gate is comparing "+
			"against nothing", len(functions))
	}

	renames := dataSourceFieldRenames()

	server := providerserver.NewProtocol6(tfprovider.New("0.0.0-test")())()
	resp, err := server.GetProviderSchema(ctx, &tfprotov6.GetProviderSchemaRequest{})
	if err != nil {
		t.Fatalf("GetProviderSchema: %v", err)
	}
	if len(resp.DataSourceSchemas) < 10 {
		t.Fatalf("only %d data sources read from the Terraform provider; the accessors "+
			"have probably changed rather than the provider having shrunk",
			len(resp.DataSourceSchemas))
	}

	var checked int
	for tfName, s := range resp.DataSourceSchemas {
		if s == nil || s.Block == nil {
			continue
		}
		token := functionTokenFor(functions, tfName)
		if token == "" {
			t.Errorf("data source %s has no function in the committed schema at all; "+
				"regenerate it:\n  cd pulumi-provider && go run "+
				"./cmd/pulumi-tfgen-terrapod schema --out cmd/pulumi-resource-terrapod",
				tfName)
			continue
		}
		entry, _ := functions[token].(map[string]any)
		outputs, _ := entry["outputs"].(map[string]any)
		props, _ := outputs["properties"].(map[string]any)

		var missing []string
		for _, a := range s.Block.Attributes {
			if a.Name == "id" {
				continue
			}
			want := pulumiName(a.Name)
			if r, ok := renames[tfName][a.Name]; ok {
				want = r
			}
			if _, ok := props[want]; ok {
				checked++
				continue
			}
			// A list or set may arrive pluralised. Accept any property that
			// EXTENDS the camelCase name, which covers the bridge's pluraliser
			// without reimplementing it -- and only for a collection, so a
			// scalar still has to match exactly.
			if isCollection(a) && hasPropExtending(props, want) {
				checked++
				continue
			}
			missing = append(missing, a.Name+" ("+want+")")
		}
		if len(missing) > 0 {
			sort.Strings(missing)
			t.Errorf("%s: the committed schema's %s is missing %d attribute(s): %s\n"+
				"  A Pulumi practitioner cannot read them. Regenerate:\n"+
				"  cd pulumi-provider && go run ./cmd/pulumi-tfgen-terrapod schema "+
				"--out cmd/pulumi-resource-terrapod",
				tfName, token, len(missing), strings.Join(missing, ", "))
		}
	}

	// A floor, so a matcher that stops matching fails rather than passing on
	// nothing. 100 is comfortably under the ~170 attributes present when this
	// was written and comfortably over anything a real shrink would leave.
	if checked < 100 {
		t.Fatalf("only %d data-source attributes compared; expected at least 100. "+
			"The accessors or the token matcher have stopped matching.", checked)
	}
	t.Logf("compared %d attributes across %d data sources", checked, len(resp.DataSourceSchemas))
}

// functionTokenFor maps `terrapod_workspace_cost` to
// `terrapod:index/getWorkspaceCost:getWorkspaceCost`, by its tail rather than by
// rebuilding the token -- which is the token strategy's business, not this
// gate's.
func functionTokenFor(functions map[string]any, tfName string) string {
	want := "get" + strings.ReplaceAll(strings.TrimPrefix(tfName, "terrapod_"), "_", "")
	for token := range functions {
		i := strings.LastIndex(token, ":")
		if i < 0 {
			continue
		}
		if strings.EqualFold(token[i+1:], want) {
			return token
		}
	}
	return ""
}

// dataSourceFieldRenames reads the renames out of our OWN bridge configuration,
// so a second one configured there needs no change here. Keyed
// [terraform data source][terraform attribute] -> pulumi name.
func dataSourceFieldRenames() map[string]map[string]string {
	out := map[string]map[string]string{}
	for tfName, ds := range Provider("0.0.0-test").DataSources {
		if ds == nil {
			continue
		}
		for attr, info := range ds.Fields {
			if info == nil || info.Name == "" {
				continue
			}
			if out[tfName] == nil {
				out[tfName] = map[string]string{}
			}
			out[tfName][attr] = info.Name
		}
	}
	return out
}

// isCollection answers whether the bridge may pluralise this attribute's name.
//
// Both shapes have to be handled and only one of them has a `Type`: a NESTED
// attribute carries `NestedType` and leaves `Type` nil, which panics a bare
// `a.Type.Is(...)`. That is not hypothetical -- it panicked on the first run of
// this gate.
func isCollection(a *tfprotov6.SchemaAttribute) bool {
	if a == nil {
		return false
	}
	if a.NestedType != nil {
		return a.NestedType.Nesting == tfprotov6.SchemaObjectNestingModeList ||
			a.NestedType.Nesting == tfprotov6.SchemaObjectNestingModeSet
	}
	if a.Type == nil {
		return false
	}
	return a.Type.Is(tftypes.List{}) || a.Type.Is(tftypes.Set{})
}

func hasPropExtending(props map[string]any, want string) bool {
	for p := range props {
		if len(p) > len(want) && strings.HasPrefix(p, want) {
			return true
		}
	}
	return false
}
