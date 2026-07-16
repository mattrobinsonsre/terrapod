// Package clean is the deterministic, AI-free cleanup of the config that
// `tofu plan -generate-config-out` emits for a set of import blocks (#824 D3).
//
// The generator is deliberately exhaustive: it writes *every* attribute of the
// resource at its zero value, including mutually-exclusive (ConflictsWith) pairs
// and Computed-only (read-only) attributes. That config is valid to write but
// fails the very next plan — e.g. AWS refuses `ipv6_cidr_block = ""` alongside
// `ipv6_netmask_length = 0` ("Conflicting configuration arguments"), and setting
// a Computed-only attribute like `tags_all` errors outright.
//
// This pass makes the config plan import-only (0 add / 0 change / 0 destroy)
// using nothing but the provider schema's required/optional/computed flags — no
// AI, and without needing the ConflictsWith metadata (which `tofu providers
// schema -json` does not even expose). The rules, validated against real
// `-generate-config-out` output:
//
//  1. Drop Computed-only attributes (computed && !optional && !required). They
//     cannot be set; leaving them errors. This is what removes `tags_all`.
//  2. Drop zero-valued Optional attributes (null / "" / 0 / false / [] / {}).
//     This is what removes the conflicting `ipv6_*` pair — both were zero — and
//     clears the rest of the generator's noise.
//  3. Keep Required attributes and non-zero Optional attributes: they came from
//     actual state, so they match the imported resource and produce no diff.
//
// When the optional AI mode is enabled it layers on top of this purely for a
// nicer diff (naming, grouping, comments) — never for correctness. With AI off,
// this pass alone yields an installable, import-only config.
package clean

import (
	"fmt"
	"sort"

	"github.com/hashicorp/hcl/v2"
	"github.com/hashicorp/hcl/v2/hclsyntax"
	"github.com/hashicorp/hcl/v2/hclwrite"
	"github.com/zclconf/go-cty/cty"

	"github.com/mattrobinsonsre/terrapod/query/internal/schema"
)

// Report summarises what the pass changed, for logging + the UI.
type Report struct {
	// Resources is the number of resource blocks visited.
	Resources int `json:"resources"`
	// RemovedComputed / RemovedZero count attributes pruned by each rule.
	RemovedComputed int `json:"removed_computed"`
	RemovedZero     int `json:"removed_zero"`
	// UnknownTypes lists resource types absent from the provided schema; their
	// blocks are left untouched (we cannot classify their attributes).
	UnknownTypes []string `json:"unknown_types,omitempty"`
}

// Clean rewrites the `-generate-config-out` HCL in src, pruning per the package
// rules, and returns the cleaned HCL plus a Report. It never removes Required
// attributes or attributes whose type it cannot classify (unknown resource
// types are left verbatim), so the worst case is a config that still needs a
// human — never one that silently drops a real, required value.
func Clean(src []byte, sch *schema.Schema) ([]byte, Report, error) {
	f, diags := hclwrite.ParseConfig(src, "generated.tf", hcl.Pos{Line: 1, Column: 1})
	if diags.HasErrors() {
		return nil, Report{}, fmt.Errorf("parse generated config: %s", diags.Error())
	}

	// Index resource-type attribute schemas across all providers. Two providers
	// theoretically could share a resource-type name; the schema map is keyed by
	// type, and generate-config-out output is unambiguous per type, so a flat
	// index is correct for this use.
	resAttrs := indexResourceSchemas(sch)

	rep := Report{}
	unknown := map[string]bool{}
	for _, block := range f.Body().Blocks() {
		if block.Type() != "resource" {
			continue
		}
		labels := block.Labels()
		if len(labels) < 1 {
			continue
		}
		rtype := labels[0]
		rep.Resources++
		blk, ok := resAttrs[rtype]
		if !ok {
			unknown[rtype] = true
			continue // unknown type — leave it untouched
		}
		cleanBody(block.Body(), blk, &rep)
	}

	for t := range unknown {
		rep.UnknownTypes = append(rep.UnknownTypes, t)
	}
	sort.Strings(rep.UnknownTypes)
	return f.Bytes(), rep, nil
}

// cleanBody prunes one block body (a resource, or a nested block) against its
// schema Block. Nested blocks recurse; a nested block left empty is removed.
func cleanBody(body *hclwrite.Body, blk schema.Block, rep *Report) {
	for name, attr := range body.Attributes() {
		sa, ok := blk.Attributes[name]
		if !ok {
			continue // not a schema attribute (unusual) — keep, be conservative
		}
		if sa.Required {
			continue // never drop a required argument
		}
		if sa.Computed && !sa.Optional {
			body.RemoveAttribute(name)
			rep.RemovedComputed++
			continue
		}
		zero, ok := exprIsZero(attr)
		if ok && zero {
			body.RemoveAttribute(name)
			rep.RemovedZero++
		}
	}

	// Recurse into nested blocks (e.g. `timeouts {}`, repeatable `ingress {}`).
	// After cleaning, drop a nested block that has become empty — the generator
	// emits empty computed/optional blocks that only add noise.
	for _, nb := range body.Blocks() {
		sub, ok := blk.BlockTypes[nb.Type()]
		if !ok {
			continue
		}
		cleanBody(nb.Body(), sub.Block, rep)
		if bodyIsEmpty(nb.Body()) {
			body.RemoveBlock(nb)
		}
	}
}

func bodyIsEmpty(body *hclwrite.Body) bool {
	return len(body.Attributes()) == 0 && len(body.Blocks()) == 0
}

// exprIsZero evaluates an attribute's expression (generate-config-out emits only
// literals, so no evaluation context is needed) and reports whether it is the
// zero value for its type. The second return is false when the expression can't
// be evaluated as a literal (a reference/function) — such an attribute is kept.
func exprIsZero(attr *hclwrite.Attribute) (zero bool, ok bool) {
	tokens := attr.Expr().BuildTokens(nil)
	expr, diags := hclsyntax.ParseExpression(tokens.Bytes(), "", hcl.InitialPos)
	if diags.HasErrors() {
		return false, false
	}
	val, diags := expr.Value(nil)
	if diags.HasErrors() {
		return false, false // not a literal (has references) — keep it
	}
	return valueIsZero(val), true
}

func valueIsZero(v cty.Value) bool {
	if v.IsNull() {
		return true
	}
	if !v.IsKnown() {
		return false
	}
	t := v.Type()
	switch {
	case t == cty.String:
		return v.AsString() == ""
	case t == cty.Number:
		return v.AsBigFloat().Sign() == 0
	case t == cty.Bool:
		return v.False()
	case t.IsListType() || t.IsSetType() || t.IsTupleType() || t.IsMapType() || t.IsObjectType():
		return v.LengthInt() == 0
	}
	return false
}

// indexResourceSchemas flattens every provider's resource schemas into a
// type→Block map for attribute classification.
func indexResourceSchemas(sch *schema.Schema) map[string]schema.Block {
	out := map[string]schema.Block{}
	if sch == nil {
		return out
	}
	for _, prov := range sch.ProviderSchemas {
		for rtype, rs := range prov.ResourceSchemas {
			out[rtype] = rs.Block
		}
	}
	return out
}
