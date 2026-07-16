// Package importblock turns a discovery query's structured result into
// candidate OpenTofu `import {}` blocks.
//
// This is deliverable D3 of #823. It maps the ids a data source returned onto
// the managed resource type they'd be imported into, and emits an `import`
// block per id. It performs no import — it emits blocks a caller reviews and,
// in #824, feeds to `tofu plan -generate-config-out` behind the import-only
// plan gate.
//
// # The honest caveat
//
// A data source returns each resource's `id`, but the *import identifier* is
// not always that `id` — some resources import by a composite key, an ARN, or
// a `name:region` tuple. Id derivation here is therefore best-effort: it uses
// the returned `id`/`ids`, which is correct for the large majority of
// resources (VPCs, subnets, instances, security groups, volumes, …). A
// mis-derived id fails safe: the import-only plan gate in #824 renders it as a
// create/replace instead of an import, which the operator sees and does not
// merge. Wrong guesses never mutate infrastructure.
package importblock

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"unicode"

	"github.com/mattrobinsonsre/terrapod/query/internal/query"
)

// Block is a single candidate import.
type Block struct {
	// To is the resource address, "<type>.<name>", e.g. "aws_vpc.vpc_0abc".
	To string `json:"to"`
	// ID is the import identifier passed to the provider.
	ID string `json:"id"`
}

// HCL renders the block as an `import {}` block.
func (b Block) HCL() string {
	return fmt.Sprintf("import {\n  to = %s\n  id = %q\n}\n", b.To, b.ID)
}

// Options controls emission.
type Options struct {
	// ResourceType is the managed resource type to import into (e.g.
	// "aws_vpc"). When empty it is derived from the data-source type by
	// singularising it — best-effort; set it explicitly when the data source
	// name doesn't singularise to the resource name.
	ResourceType string
}

// FromResult derives candidate import blocks from a query result. A plural/list
// result (a computed `ids` array) yields one block per id; a singular result (a
// computed `id`) yields one block. An empty result yields no blocks.
func FromResult(res *query.Result, opts Options) ([]Block, error) {
	rtype := opts.ResourceType
	if rtype == "" {
		rtype = singularize(res.Type)
	}

	ids, err := extractIDs(res.Value)
	if err != nil {
		return nil, err
	}

	blocks := make([]Block, 0, len(ids))
	used := map[string]bool{}
	for _, id := range ids {
		name := uniqueName(sanitizeName(id), used)
		blocks = append(blocks, Block{To: rtype + "." + name, ID: id})
	}
	return blocks, nil
}

// Render returns the HCL for a set of blocks, one after another.
func Render(blocks []Block) string {
	var b strings.Builder
	for i, blk := range blocks {
		if i > 0 {
			b.WriteString("\n")
		}
		b.WriteString(blk.HCL())
	}
	return b.String()
}

// extractIDs pulls the id(s) from a data source's output value. It mirrors the
// schema-side rule in schema.idListAttr so the surface and the import stage agree
// on which attribute is the id list: the canonical `ids` list, else the single
// attribute named `*_ids`/`*_identifiers` that is a list of strings (aws_eips →
// `allocation_ids`), else the scalar `id` (a genuinely singular source). The
// synthetic scalar `id` that plural sources also carry (a region/hash) is never
// reached for them because their `*_ids` list is picked first — and such sources
// are only surfaced when that list exists.
func extractIDs(value json.RawMessage) ([]string, error) {
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(value, &obj); err != nil {
		return nil, fmt.Errorf("result value is not an object: %w", err)
	}
	if raw, ok := obj["ids"]; ok {
		var ids []string
		if err := json.Unmarshal(raw, &ids); err != nil {
			return nil, fmt.Errorf("ids is not a string list: %w", err)
		}
		return ids, nil
	}
	// A single `*_ids` / `*_identifiers` string list (e.g. allocation_ids).
	var candidate string
	n := 0
	for k, raw := range obj {
		if !strings.HasSuffix(k, "_ids") && !strings.HasSuffix(k, "_identifiers") {
			continue
		}
		var ids []string
		if json.Unmarshal(raw, &ids) == nil {
			candidate = k
			n++
		}
	}
	if n == 1 {
		var ids []string
		_ = json.Unmarshal(obj[candidate], &ids)
		return ids, nil
	}
	if raw, ok := obj["id"]; ok {
		var id string
		if err := json.Unmarshal(raw, &id); err == nil && id != "" {
			return []string{id}, nil
		}
	}
	return nil, nil
}

// singularize best-effort-maps a data-source type to its managed resource type
// by trimming a single trailing "s" (aws_vpcs → aws_vpc, aws_subnets →
// aws_subnet). Callers override via Options.ResourceType when this is wrong.
func singularize(dsType string) string {
	if s, ok := strings.CutSuffix(dsType, "s"); ok {
		return s
	}
	return dsType
}

// sanitizeName turns a resource id into a valid Terraform resource name
// (letters, digits, underscores, dashes; must start with a letter or
// underscore).
func sanitizeName(id string) string {
	var b strings.Builder
	for _, r := range id {
		if r == '-' || r == '_' || unicode.IsLetter(r) || unicode.IsDigit(r) {
			b.WriteRune(r)
		} else {
			b.WriteRune('_')
		}
	}
	s := b.String()
	if s == "" {
		return "imported"
	}
	if r0 := rune(s[0]); !unicode.IsLetter(r0) && r0 != '_' {
		s = "r_" + s
	}
	return s
}

// uniqueName disambiguates collisions by appending _2, _3, ….
func uniqueName(base string, used map[string]bool) string {
	name := base
	for i := 2; used[name]; i++ {
		name = fmt.Sprintf("%s_%d", base, i)
	}
	used[name] = true
	return name
}

// SortBlocks orders blocks by address for stable output.
func SortBlocks(blocks []Block) {
	sort.Slice(blocks, func(i, j int) bool { return blocks[i].To < blocks[j].To })
}
