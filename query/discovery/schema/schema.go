// Package schema parses `tofu providers schema -json` into the discovery
// surface terrapod-query exposes: the set of data sources a caller can run to
// find existing resources, and — for each — the arguments it accepts to narrow
// the search.
//
// This is deliverable D1 of #823. It decides nothing about strategy (that is
// #824's AI) and holds no hardcoded per-resource knowledge (unlike a fixed
// provider allowlist) — every fact comes from the provider's own schema, so a
// new provider or a new data source is understood for free.
package schema

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
)

// Schema is the top-level `providers schema -json` document.
type Schema struct {
	FormatVersion   string              `json:"format_version"`
	ProviderSchemas map[string]Provider `json:"provider_schemas"`
}

// Provider holds one provider's resource and data-source schemas. The map key
// in ProviderSchemas is the fully-qualified source address, e.g.
// "registry.opentofu.org/hashicorp/aws".
type Provider struct {
	DataSourceSchemas map[string]ResourceSchema `json:"data_source_schemas"`
	ResourceSchemas   map[string]ResourceSchema `json:"resource_schemas"`
}

// ResourceSchema is the schema of a single resource or data source.
type ResourceSchema struct {
	Version int64 `json:"version"`
	Block   Block `json:"block"`
}

// Block is a configuration block: a set of scalar attributes plus nested
// blocks (block_types).
type Block struct {
	Attributes map[string]Attribute `json:"attributes"`
	BlockTypes map[string]BlockType `json:"block_types"`
}

// Attribute is a single argument/attribute. Optional (and not Computed) means
// the caller can set it — i.e. it is a query input. Computed-only attributes
// are outputs the data source returns.
type Attribute struct {
	// Type is left as raw JSON: it may be a string ("string") or a nested
	// array (["list","string"]), and D1 doesn't need to interpret it.
	Type        json.RawMessage `json:"type"`
	Description string          `json:"description"`
	Required    bool            `json:"required"`
	Optional    bool            `json:"optional"`
	Computed    bool            `json:"computed"`
}

// BlockType is a nested block (e.g. a repeatable `filter {}` block).
type BlockType struct {
	NestingMode string `json:"nesting_mode"`
	Block       Block  `json:"block"`
}

// Parse decodes a `providers schema -json` document.
func Parse(b []byte) (*Schema, error) {
	var s Schema
	if err := json.Unmarshal(b, &s); err != nil {
		return nil, fmt.Errorf("parse providers schema: %w", err)
	}
	return &s, nil
}

// DataSource describes one data source as a discovery candidate: which narrowing
// signals it exposes and what a query can constrain on. It is the unit #824's AI
// reasons over when choosing what to try.
type DataSource struct {
	// Provider is the fully-qualified provider source address.
	Provider string `json:"provider"`
	// Name is the data-source type, e.g. "aws_vpcs".
	Name string `json:"name"`
	// HasFilter is true when the data source exposes a `filter {}` block — the
	// canonical EC2-style narrowing mechanism.
	HasFilter bool `json:"has_filter"`
	// HasTags is true when the data source accepts a settable `tags` argument.
	HasTags bool `json:"has_tags"`
	// ReturnsList is true when the data source returns multiple results (a
	// plural/list source, detected by a computed resource-id list — see
	// IDListAttr).
	ReturnsList bool `json:"returns_list"`
	// IDListAttr is the name of the computed attribute holding this source's
	// list of resource ids (the values usable as import ids), or "" when there
	// isn't an unambiguous one. It is the canonical `ids`, else the single
	// computed string-list attribute named `*_ids`/`*_identifiers` (e.g.
	// aws_eips → `allocation_ids`). This is the schema-side half of import-id
	// derivation; importblock.extractIDs applies the same rule to the query
	// result JSON, and the import-only plan is the final authority on whether the
	// chosen id was correct.
	IDListAttr string `json:"id_list_attr,omitempty"`
	// ResourceType is the managed resource these ids import into, derived by
	// singularising the data-source name (aws_eips → aws_eip) AND confirmed to
	// exist in the provider's `resource_schemas`. There is no declared
	// data-source→resource link in the schema, so the *name* is a convention
	// guess — but the *existence* check is authoritative: it rejects list sources
	// with no matching managed resource (aws_availability_zones, aws_ip_ranges).
	// "" when the singularised name isn't a real managed resource.
	ResourceType string `json:"resource_type,omitempty"`
	// Inputs are the settable (Optional or Required, non-Computed) argument
	// names — attributes and nested blocks — a query can constrain on, sorted.
	Inputs []string `json:"inputs"`
	// RequiredInputs are the subset of Inputs that are Required (must be set
	// for the data source to read), sorted. A data source whose only required
	// input is an exact id is a poor discovery candidate on its own.
	RequiredInputs []string `json:"required_inputs"`
}

// Queryable reports whether the data source has a strong, unambiguous discovery
// signal: a filter block, a tags argument, or a plural/list return. These are
// the high-confidence surface. Data sources that only accept, say, an optional
// name still appear in Catalogue (with their Inputs) so #824 can consider them,
// but they are not asserted as strong candidates here.
func (d DataSource) Queryable() bool {
	return d.HasFilter || d.HasTags || d.ReturnsList
}

// Importable reports whether the deterministic import derivation
// (query/discovery/importblock) can turn this data source's result into `import`
// blocks: it must expose a computed list of resource ids. This is deliberately
// stricter than Queryable — a source can be a fine *query* target yet have no
// usable id list. The id list is found schema-driven, with no per-resource map:
// the canonical `ids`, or the single computed string-list attribute named
// `*_ids`/`*_identifiers` (so aws_eips → `allocation_ids`, aws_db_instances →
// `instance_identifiers`, … are all handled). A source with no such list, or an
// ambiguous choice of several, is not importable and is kept off the onboarding
// surface so a user can't select a dead end.
func (d DataSource) Importable() bool {
	return d.IDListAttr != "" && d.ResourceType != ""
}

// Catalogue returns every data source across every provider, classified, sorted
// by provider then name — the complete discovery surface derived from the
// schema. Use QueryableDataSources for just the strong-signal subset.
func (s *Schema) Catalogue() []DataSource {
	var out []DataSource
	for provider, ps := range s.ProviderSchemas {
		for name, ds := range ps.DataSourceSchemas {
			out = append(out, classify(provider, name, ds, ps.ResourceSchemas))
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Provider != out[j].Provider {
			return out[i].Provider < out[j].Provider
		}
		return out[i].Name < out[j].Name
	})
	return out
}

// QueryableDataSources returns the Catalogue filtered to data sources with a
// strong discovery signal (see DataSource.Queryable).
func (s *Schema) QueryableDataSources() []DataSource {
	all := s.Catalogue()
	out := all[:0:0]
	for _, d := range all {
		if d.Queryable() {
			out = append(out, d)
		}
	}
	return out
}

// ImportableDataSources returns the Catalogue filtered to data sources the
// deterministic import path can consume (see DataSource.Importable). This is the
// onboarding surface: every entry can be queried AND turned into import blocks,
// so a user never selects a source that silently finds nothing.
func (s *Schema) ImportableDataSources() []DataSource {
	all := s.Catalogue()
	out := all[:0:0]
	for _, d := range all {
		if d.Importable() {
			out = append(out, d)
		}
	}
	return out
}

// classify derives the discovery signals for one data source from its block.
// resources is the provider's managed-resource schema map, used to confirm the
// import target actually exists.
func classify(provider, name string, ds ResourceSchema, resources map[string]ResourceSchema) DataSource {
	d := DataSource{Provider: provider, Name: name}
	blk := ds.Block

	if _, ok := blk.BlockTypes["filter"]; ok {
		d.HasFilter = true
	}
	// A settable `tags` argument (not a computed-only tags output).
	if a, ok := blk.Attributes["tags"]; ok && !a.Computed {
		d.HasTags = true
	}
	// Plural/list sources expose a computed list of matched resource ids — the
	// canonical `ids` or a single `*_ids`/`*_identifiers` string list.
	d.IDListAttr = idListAttr(blk)
	d.ReturnsList = d.IDListAttr != ""
	// Convention-guess the managed resource type (trim trailing "s"), then
	// authoritatively confirm it exists as a real managed resource.
	if rt := singularize(name); rt != name {
		if _, ok := resources[rt]; ok {
			d.ResourceType = rt
		}
	}

	// Settable inputs: optional/required non-computed attributes, plus nested
	// blocks (a block a caller can populate, e.g. `filter`).
	for aname, a := range blk.Attributes {
		if a.Computed && !a.Optional && !a.Required {
			continue // pure output
		}
		d.Inputs = append(d.Inputs, aname)
		if a.Required {
			d.RequiredInputs = append(d.RequiredInputs, aname)
		}
	}
	for bname := range blk.BlockTypes {
		d.Inputs = append(d.Inputs, bname)
	}
	sort.Strings(d.Inputs)
	sort.Strings(d.RequiredInputs)
	return d
}

// idListAttr picks the computed attribute holding a source's list of resource
// ids, schema-driven and with no per-resource map: the canonical `ids`, else the
// single computed string-list attribute whose name ends `_ids`/`_identifiers`
// (aws_eips → `allocation_ids`, aws_db_instances → `instance_identifiers`). It
// returns "" when there is none or the choice is ambiguous (several candidates),
// so such a source is neither surfaced nor mis-derived. importblock.extractIDs
// mirrors this rule against the query result JSON.
func idListAttr(blk Block) string {
	if a, ok := blk.Attributes["ids"]; ok && a.Computed && isStringList(a.Type) {
		return "ids"
	}
	var found string
	n := 0
	for name, a := range blk.Attributes {
		if !a.Computed || !isStringList(a.Type) {
			continue
		}
		if strings.HasSuffix(name, "_ids") || strings.HasSuffix(name, "_identifiers") {
			found = name
			n++
		}
	}
	if n == 1 {
		return found
	}
	return ""
}

// isStringList reports whether a raw attribute type is a `list(string)` or
// `set(string)`, encoded as ["list","string"] / ["set","string"].
func isStringList(t json.RawMessage) bool {
	var arr []json.RawMessage
	if json.Unmarshal(t, &arr) != nil || len(arr) != 2 {
		return false
	}
	var kind, elem string
	if json.Unmarshal(arr[0], &kind) != nil || json.Unmarshal(arr[1], &elem) != nil {
		return false
	}
	return (kind == "list" || kind == "set") && elem == "string"
}

// singularize best-effort-maps a data-source type to its managed resource type
// by trimming a single trailing "s" (aws_vpcs → aws_vpc). There is no declared
// data-source→resource link in the provider schema, so this is a naming
// convention; callers confirm the result exists in resource_schemas before
// trusting it, and mirrors importblock.singularize.
func singularize(dsType string) string {
	if s, ok := strings.CutSuffix(dsType, "s"); ok {
		return s
	}
	return dsType
}
