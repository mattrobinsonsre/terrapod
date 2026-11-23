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

	tf := terraformResourceFields(ctx, t)
	if len(tf) < 20 {
		t.Fatalf("only %d resources read from the Terraform provider; the accessors have "+
			"probably changed rather than the provider having shrunk", len(tf))
	}

	var checked, checkedBlocks int
	for tfName, fields := range tf {
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
		for _, f := range fields {
			if _, ok := props[pulumiName(f.name)]; ok {
				continue
			}
			// A collection whose name is not already plural is PLURALISED by
			// the bridge -- a `key` block arrives as `keys` -- so accept an
			// extension of the name for one, exactly as the data-source half
			// does. Not for a scalar, where an extension would be a different
			// property.
			if f.collection && hasPropExtending(props, pulumiName(f.name)) {
				continue
			}
			missing = append(missing, f.name+" ("+pulumiName(f.name)+")")
		}
		checked += len(fields)
		for _, f := range fields {
			if f.block {
				checkedBlocks++
			}
		}
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
	// And a separate floor on the BLOCKS, because the one above cannot see them
	// disappear: there are hundreds of attributes and one block, so a derivation
	// that stopped reading `Block.BlockTypes` entirely would leave `checked` far
	// above 200 and this gate green while the surface it was extended to cover
	// went unchecked. One resource uses a block today (#2592).
	if checkedBlocks < 1 {
		t.Fatal("no block was compared; the derivation is not reading " +
			"Block.BlockTypes, so a block reaching the committed schema is unchecked")
	}
	t.Logf("compared %d fields (%d of them blocks) across %d resources",
		checked, checkedBlocks, len(tf))
}

// resourceField is one top-level thing a practitioner can set on a resource:
// an attribute, or a BLOCK.
//
// `collection` decides whether a pluralised name in the committed schema counts
// as the same field, because the bridge pluralises a list- or set-typed
// property whose name is not already plural.
type resourceField struct {
	name       string
	collection bool
	// block says this field is a nested block rather than an attribute, so the
	// gate can floor on them separately -- hundreds of attributes would hide
	// the loss of the one block otherwise.
	block bool
}

// terraformResourceFields reads each resource's top-level attribute AND BLOCK
// names straight off the Terraform provider, through the protocol server -- the
// same path `terraform` itself uses, so there is no second opinion about what
// the provider declares.
//
// **Blocks are included, and that is the point.** This gate read
// `Block.Attributes` alone until #2592, which made `terrapod_inventory_host_keys`
// the first resource to use a block -- so its `key` block, and everything
// inside it, would have reached the committed schema or not with every gate
// green. That is the same shape as the defect this file's header describes, one
// level down: a derived gate's derivation decides its blind spot, and a block is
// as much a public surface as an attribute.
//
// The block's own nested attributes are the sibling gate's subject
// (TestEveryTerraformBlockAttributeReachesTheCommittedSchema) -- they live in a
// generated TYPE rather than in `inputProperties`, so comparing them needs a
// `$ref` to follow and a separate message to report.
func terraformResourceFields(ctx context.Context, t *testing.T) map[string][]resourceField {
	t.Helper()

	server := providerserver.NewProtocol6(tfprovider.New("0.0.0-test")())()
	resp, err := server.GetProviderSchema(ctx, &tfprotov6.GetProviderSchemaRequest{})
	if err != nil {
		t.Fatalf("GetProviderSchema: %v", err)
	}

	out := make(map[string][]resourceField, len(resp.ResourceSchemas))
	for name, s := range resp.ResourceSchemas {
		if s == nil || s.Block == nil {
			continue
		}
		fields := make([]resourceField, 0, len(s.Block.Attributes)+len(s.Block.BlockTypes))
		for _, a := range s.Block.Attributes {
			// `id` is Pulumi's own resource identity and is not an input
			// property, so it is not drift when it is absent.
			if a.Name == "id" || a.Computed && !a.Optional && !a.Required {
				continue
			}
			fields = append(fields, resourceField{name: a.Name, collection: isCollection(a)})
		}
		for _, b := range s.Block.BlockTypes {
			fields = append(fields, resourceField{
				name: b.TypeName, collection: isCollectionBlock(b), block: true,
			})
		}
		out[name] = fields
	}
	return out
}

// isCollectionBlock reports whether a nested block is repeated, and so arrives
// in Pulumi as an array under a possibly-pluralised name.
func isCollectionBlock(b *tfprotov6.SchemaNestedBlock) bool {
	if b == nil {
		return false
	}
	return b.Nesting == tfprotov6.SchemaNestedBlockNestingModeList ||
		b.Nesting == tfprotov6.SchemaNestedBlockNestingModeSet
}

// TestEveryTerraformBlockAttributeReachesTheCommittedSchema is the other half of
// the gate above: a block's own ATTRIBUTES.
//
// The sibling gate asks whether the block itself reached `inputProperties`. That
// can be true while everything inside it is missing, because a block's contents
// land in a generated TYPE that `inputProperties` only references -- so a
// practitioner could declare `keys` and set nothing in it, which is the same
// unusable surface the header describes for a whole resource.
//
// Derived from the provider, with a floor, and it follows the schema's own
// `$ref` rather than rebuilding the type token -- which is the token strategy's
// business, exactly as `tokenFor` is careful not to rebuild a resource's.
func TestEveryTerraformBlockAttributeReachesTheCommittedSchema(t *testing.T) {
	ctx := context.Background()
	committed := theSchema(t)
	resources, _ := committed["resources"].(map[string]any)
	schemaTypes, _ := committed["types"].(map[string]any)

	server := providerserver.NewProtocol6(tfprovider.New("0.0.0-test")())()
	resp, err := server.GetProviderSchema(ctx, &tfprotov6.GetProviderSchemaRequest{})
	if err != nil {
		t.Fatalf("GetProviderSchema: %v", err)
	}

	var checkedBlocks, checkedAttrs int
	for tfName, s := range resp.ResourceSchemas {
		if s == nil || s.Block == nil || len(s.Block.BlockTypes) == 0 {
			continue
		}
		token := tokenFor(resources, tfName)
		if token == "" {
			continue
		}
		entry, _ := resources[token].(map[string]any)
		props, _ := entry["inputProperties"].(map[string]any)

		for _, b := range s.Block.BlockTypes {
			if b == nil || b.Block == nil {
				continue
			}
			typeProps := blockTypeProperties(props, schemaTypes, pulumiName(b.TypeName))
			if typeProps == nil {
				// The block reached no type at all. The sibling gate reports
				// the block being absent, so say something different: this is
				// a block that is present and empty.
				t.Errorf("%s: block %q carries no generated type in the committed schema, so a "+
					"Pulumi practitioner can name it and set nothing in it. Regenerate it with "+
					"`go run ./cmd/pulumi-tfgen-terrapod schema --out cmd/pulumi-resource-terrapod`.",
					tfName, b.TypeName)
				continue
			}
			checkedBlocks++

			var missing []string
			for _, a := range b.Block.Attributes {
				if a.Computed && !a.Optional && !a.Required {
					continue
				}
				checkedAttrs++
				if _, ok := typeProps[pulumiName(a.Name)]; ok {
					continue
				}
				if isCollection(a) && hasPropExtending(typeProps, pulumiName(a.Name)) {
					continue
				}
				missing = append(missing, a.Name+" ("+pulumiName(a.Name)+")")
			}
			if len(missing) > 0 {
				sort.Strings(missing)
				t.Errorf("%s: block %q is missing %d attribute(s) from its generated Pulumi "+
					"type: %s. Regenerate it with `go run ./cmd/pulumi-tfgen-terrapod schema "+
					"--out cmd/pulumi-resource-terrapod`.",
					tfName, b.TypeName, len(missing), strings.Join(missing, ", "))
			}
		}
	}

	// Floors on BOTH, because the loop skips a resource whose token it cannot
	// find and a block whose type it cannot resolve -- so either could reduce
	// this gate to checking nothing while reporting success. One resource uses
	// blocks today (#2592) and its one block carries three attributes.
	if checkedBlocks < 1 {
		t.Fatal("no block was compared; the derivation is not finding the provider's blocks")
	}
	if checkedAttrs < 3 {
		t.Fatalf("only %d block attributes compared; the derivation is not reading into the "+
			"blocks it found", checkedAttrs)
	}
	t.Logf("compared %d attributes across %d block(s)", checkedAttrs, checkedBlocks)
}

// blockTypeProperties resolves a block's generated type and returns its
// properties, or nil when the block reaches no type.
//
// The property name is looked up exactly and then as an extension, because the
// bridge pluralises a repeated block -- `key` becomes `keys`.
func blockTypeProperties(props, schemaTypes map[string]any, want string) map[string]any {
	candidates := []string{want}
	for p := range props {
		if len(p) > len(want) && strings.HasPrefix(p, want) {
			candidates = append(candidates, p)
		}
	}
	for _, name := range candidates {
		prop, ok := props[name].(map[string]any)
		if !ok {
			continue
		}
		// An array of objects references its type through `items`; a single
		// nested block references it directly.
		ref, _ := prop["$ref"].(string)
		if items, ok := prop["items"].(map[string]any); ok {
			if r, ok := items["$ref"].(string); ok {
				ref = r
			}
		}
		token := strings.TrimPrefix(ref, "#/types/")
		if token == "" || token == ref && !strings.HasPrefix(ref, "#/types/") {
			continue
		}
		entry, ok := schemaTypes[token].(map[string]any)
		if !ok {
			continue
		}
		if typeProps, ok := entry["properties"].(map[string]any); ok {
			return typeProps
		}
	}
	return nil
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

	tf := terraformResourceFields(ctx, t)
	fields := tf["terrapod_workspace"]
	if len(fields) < 40 {
		t.Fatalf("only %d fields on terrapod_workspace; the derivation is wrong", len(fields))
	}

	mapped := make(map[string]string, len(fields))
	for _, f := range fields {
		n := pulumiName(f.name)
		mapped[n] = f.name
		// tfbridge pluralises a REPEATED BLOCK whose name is not already
		// plural, because it arrives in Pulumi as an array -- so the singular
		// `ansible_collection` is emitted as `ansibleCollections` (#2681).
		// Keyed on the two flags `resourceField` already carries rather than on
		// the name alone, so a plain attribute ending in a consonant does not
		// quietly acquire a second spelling it never has.
		if f.block && f.collection && !strings.HasSuffix(n, "s") {
			mapped[n+"s"] = f.name
		}
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
