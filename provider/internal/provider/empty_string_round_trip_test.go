package provider

// The empty-string round-trip gate (#2310).
//
// An attribute that is `optional=true computed=false` and a plain string can be
// set to "" by a configuration, and the framework plans that "" as "" rather
// than as unknown. So the apply must answer "" back, and a read-back that maps
// the server's "" to null fails it with
//
//	Error: Provider produced inconsistent result after apply
//
// on the one configuration that asks for the value to be cleared. Seven closed
// issues are instances of that single class -- #684, #732, #1634, #1748, #1771,
// #2110, #2307 -- and #2310 is the eighth, found as a measurement across the
// whole provider rather than one resource at a time.
//
// #2307 deliberately shipped no gate, because a gate reading one resource's
// source would have been the "written for one instance of a class, blind to the
// rest" shape. This is the general form #2310 asked for: the subject is DERIVED
// from schema.golden crossed with the read-backs, so a nineteenth instance fails
// here rather than being found by the next person to try clearing a field.
//
// Its first run earned its keep: `terrapod_policy.description` is in this class
// and appears nowhere in #2310's hand-built table.
//
// WHAT IT CANNOT DECIDE, and why there is a ledger. Whether "" and null are
// genuinely different states for an attribute is a property of the SERVER, not
// of the provider's source: a column that stores "" for "unset" needs the
// read-back to answer "", while one that is truly nullable is right to collapse.
// Source analysis cannot tell those apart, so the ones that are legitimately
// nullable are listed below WITH A REASON, and the list is checked in both
// directions so it cannot quietly describe a state that has moved.

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// resourcesDir is where a resource's own package lives, relative to this one.
const resourcesDir = "../resources"

// Floors, so a matcher that stops matching fails here instead of passing on an
// empty subject. They are deliberately slack: the question each one answers is
// "did the derivation run at all", and a new resource is not a defect.
const (
	minOptionalOnlyStrings = 20
	minModelledPackages    = 10
)

// legitimatelyNullable lists the attributes whose read-back is RIGHT to answer
// null, keyed "<terraform resource name>.<attribute>". The value is the reason,
// and it has to be a reason about the SERVER -- what "" means to the column
// behind the attribute -- because that is the only thing this gate cannot read
// for itself.
//
// Checked in both directions by TestTheLedgerDescribesTheTreeAsItIs: an entry
// that no longer names a flagged attribute fails, so a fix elsewhere cannot
// leave a stale excuse sitting here looking considered.
var legitimatelyNullable = map[string]string{
	"terrapod_autodiscovery_rule.on_directory_delete": "an enum, so \"\" is not a storable value -- " +
		"the API answers 422 for anything outside its set, which means a configuration cannot ask " +
		"for the empty string in the first place and there is no round trip to break",

	"terrapod_catalog_item.variable_options_json": "the attribute carries a JSON document, " +
		"marshalled from the server's own structure. \"\" is not valid JSON, so the state the " +
		"practitioner can express is \"a document\" or \"no document\", and null is how the second " +
		"one is spelled",

	"terrapod_provider_template.parameters_json": "the same shape as " +
		"terrapod_catalog_item.variable_options_json: a marshalled JSON document, where \"\" is not " +
		"a value the attribute can hold",
}

// anAttribute is one row of the derived subject.
type anAttribute struct {
	resource string // terraform resource name, e.g. terrapod_policy
	attr     string // terraform attribute name, e.g. description
	pkg      string // the resource's package directory
}

func (a anAttribute) key() string { return a.resource + "." + a.attr }

// optionalOnlyStringsFromTheGolden reads the subject out of schema.golden.
//
// DATA SOURCES ARE EXCLUDED STRUCTURALLY, not by a ledger entry: a data source
// has no apply, so "inconsistent result after apply" cannot arise, and its
// optional strings are filter inputs rather than managed values. That is
// derivable from the golden's own line prefix, which is why it is not a list.
func optionalOnlyStringsFromTheGolden(t *testing.T) []anAttribute {
	t.Helper()

	raw, err := os.ReadFile(schemaGolden)
	if err != nil {
		t.Fatalf("read %s: %v", schemaGolden, err)
	}

	var out []anAttribute
	for _, line := range strings.Split(string(raw), "\n") {
		// Only managed resources. A `data ` line is the excluded class above.
		if !strings.HasPrefix(line, "resource ") {
			continue
		}
		if !strings.Contains(line, "optional=true") ||
			!strings.Contains(line, "computed=false") ||
			!strings.Contains(line, "type=basetypes.StringType") {
			continue
		}
		head := strings.Fields(strings.TrimPrefix(line, "resource "))
		if len(head) == 0 {
			continue
		}
		res, attr, ok := strings.Cut(head[0], ".")
		if !ok {
			continue
		}
		out = append(out, anAttribute{
			resource: res,
			attr:     attr,
			pkg:      filepath.Join(resourcesDir, strings.TrimPrefix(res, "terrapod_")),
		})
	}

	if len(out) < minOptionalOnlyStrings {
		t.Fatalf("derived only %d optional-only string attribute(s) from %s, want at least %d: "+
			"the golden's format has moved and this gate is no longer reading it",
			len(out), schemaGolden, minOptionalOnlyStrings)
	}
	return out
}

// aPackage is one resource package, parsed once.
type aPackage struct {
	// fieldForAttr maps a tfsdk tag to the Go field name carrying it.
	fieldForAttr map[string]string
	// nullAssigned is the set of Go field names assigned a bare
	// types.StringNull() by an assignment that is NOT guarded by a condition
	// consulting that field's own null/unknown state.
	//
	// That exclusion is the whole subtlety. The in-tree fix for this class
	// comes in two forms: a call to optionalString(configured, server), and the
	// same logic written inline --
	//
	//	if p.Description == "" && (m.Description.IsNull() || m.Description.IsUnknown()) {
	//		m.Description = types.StringNull()
	//	} else {
	//		m.Description = types.StringValue(p.Description)
	//	}
	//
	// -- which answers null only when the configuration did not ask for a
	// value, so an explicit "" still round-trips. A matcher that saw only the
	// bare types.StringNull() would report that as the defect it is the fix
	// for, and `terrapod_policy.description` is exactly that shape. Writing
	// this gate without the exclusion flagged it, and the correct response was
	// to teach the matcher rather than to "fix" a working read-back.
	nullAssigned map[string]bool
	// assigned is every Go field name assigned anything at all. A field absent
	// from this set is never written back from a server value -- a write-only
	// attribute such as a password or a token -- and cannot be inconsistent.
	assigned map[string]bool
}

// parsePackage reads one resource package's non-test sources.
func parsePackage(t *testing.T, dir string) (*aPackage, bool) {
	t.Helper()

	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, false
	}

	p := &aPackage{
		fieldForAttr: map[string]string{},
		nullAssigned: map[string]bool{},
		assigned:     map[string]bool{},
	}
	fset := token.NewFileSet()
	var guards []aGuard
	var nulls []aNullAssignment

	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		f, err := parser.ParseFile(fset, filepath.Join(dir, name), nil, 0)
		if err != nil {
			t.Fatalf("parse %s/%s: %v", dir, name, err)
		}

		ast.Inspect(f, func(n ast.Node) bool {
			switch node := n.(type) {
			// Every struct field carrying a tfsdk tag, from any struct in the
			// package -- a resource may model nested blocks separately, and the
			// union is what the attribute names have to be looked up against.
			case *ast.StructType:
				for _, fld := range node.Fields.List {
					if fld.Tag == nil || len(fld.Names) != 1 {
						continue
					}
					tag, err := strconv.Unquote(fld.Tag.Value)
					if err != nil {
						continue
					}
					if tfsdk := reflect.StructTag(tag).Get("tfsdk"); tfsdk != "" {
						p.fieldForAttr[tfsdk] = fld.Names[0].Name
					}
				}

			// An `if` whose condition consults a model field's own
			// null/unknown state guards everything inside it for that field:
			// a types.StringNull() in there is the inline form of
			// optionalString, not a collapse.
			case *ast.IfStmt:
				for _, field := range fieldsConsultedForEmptiness(node.Cond) {
					guards = append(guards, aGuard{field: field, from: node.Pos(), to: node.End()})
				}

			// Assignments to a model field. The receiver name is deliberately
			// ignored: packages variously call it m, state, plan or data, and
			// the question here is about the FIELD.
			case *ast.AssignStmt:
				for i, lhs := range node.Lhs {
					sel, ok := lhs.(*ast.SelectorExpr)
					if !ok {
						continue
					}
					field := sel.Sel.Name
					p.assigned[field] = true
					if i < len(node.Rhs) && isBareStringNull(node.Rhs[i]) {
						nulls = append(nulls, aNullAssignment{field: field, at: lhs.Pos()})
					}
				}
			}
			return true
		})
	}

	// A bare null counts only where nothing guarded it.
	for _, n := range nulls {
		if !guardedBy(guards, n) {
			p.nullAssigned[n.field] = true
		}
	}
	return p, true
}

// aGuard is one `if` that consults a field's own null/unknown state, spanning
// the whole statement so either branch is covered.
type aGuard struct {
	field    string
	from, to token.Pos
}

// aNullAssignment is one bare types.StringNull() written to a model field.
type aNullAssignment struct {
	field string
	at    token.Pos
}

func guardedBy(guards []aGuard, n aNullAssignment) bool {
	for _, g := range guards {
		if g.field == n.field && n.at >= g.from && n.at < g.to {
			return true
		}
	}
	return false
}

// fieldsConsultedForEmptiness returns the model fields whose IsNull() or
// IsUnknown() the expression calls -- i.e. the fields this condition is asking
// the CONFIGURATION about, rather than asking the server about.
func fieldsConsultedForEmptiness(e ast.Expr) []string {
	var out []string
	ast.Inspect(e, func(n ast.Node) bool {
		call, ok := n.(*ast.CallExpr)
		if !ok || len(call.Args) != 0 {
			return true
		}
		outer, ok := call.Fun.(*ast.SelectorExpr)
		if !ok {
			return true
		}
		if outer.Sel.Name != "IsNull" && outer.Sel.Name != "IsUnknown" {
			return true
		}
		// The receiver of IsNull()/IsUnknown() is the model field itself.
		if inner, ok := outer.X.(*ast.SelectorExpr); ok {
			out = append(out, inner.Sel.Name)
		}
		return true
	})
	return out
}

// isBareStringNull reports whether an expression is exactly types.StringNull().
//
// A read-back that routes through a helper instead -- optionalString and
// optionalStringKeepingExplicitEmpty both consult what the CONFIGURATION asked
// for as well as what the server answered -- never matches, which is correct:
// those answer null only when the config did not ask for a value, so an
// explicit "" still round-trips.
func isBareStringNull(e ast.Expr) bool {
	call, ok := e.(*ast.CallExpr)
	if !ok || len(call.Args) != 0 {
		return false
	}
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok || sel.Sel.Name != "StringNull" {
		return false
	}
	pkg, ok := sel.X.(*ast.Ident)
	return ok && pkg.Name == "types"
}

// flaggedAttributes is the shared derivation: every optional-only string whose
// read-back can answer a bare null.
func flaggedAttributes(t *testing.T) map[string]anAttribute {
	t.Helper()

	subject := optionalOnlyStringsFromTheGolden(t)
	packages := map[string]*aPackage{}
	modelled := 0

	for _, a := range subject {
		if _, seen := packages[a.pkg]; seen {
			continue
		}
		p, ok := parsePackage(t, a.pkg)
		if !ok {
			continue
		}
		packages[a.pkg] = p
		if len(p.fieldForAttr) > 0 {
			modelled++
		}
	}

	if modelled < minModelledPackages {
		t.Fatalf("found tfsdk-tagged models in only %d package(s), want at least %d: "+
			"the tag walk is no longer finding the models and this gate is inspecting nothing",
			modelled, minModelledPackages)
	}

	flagged := map[string]anAttribute{}
	for _, a := range subject {
		p, ok := packages[a.pkg]
		if !ok {
			continue
		}
		field, ok := p.fieldForAttr[a.attr]
		if !ok {
			continue
		}
		// Never written back from a server value: a write-only attribute, which
		// the server does not return and so cannot be inconsistent about.
		if !p.assigned[field] {
			continue
		}
		if p.nullAssigned[field] {
			flagged[a.key()] = a
		}
	}
	return flagged
}

// TestNoOptionalOnlyStringCollapsesAnExplicitEmptyString is the gate.
func TestNoOptionalOnlyStringCollapsesAnExplicitEmptyString(t *testing.T) {
	flagged := flaggedAttributes(t)

	var bad []string
	for key := range flagged {
		if _, excused := legitimatelyNullable[key]; !excused {
			bad = append(bad, key)
		}
	}
	sort.Strings(bad)

	if len(bad) > 0 {
		t.Errorf("%d optional-only string attribute(s) map the server's \"\" to null, so a "+
			"configuration that sets them to \"\" fails the apply with \"Provider produced "+
			"inconsistent result after apply\" (#2310):\n  %s\n\n"+
			"Fix it one of the two ways already in the tree:\n"+
			"  - Optional+Computed + UseStateForUnknown, and read the server's value back\n"+
			"    unconditionally (the house pattern -- #684, #732, #1634, #1748, #1771, #2110,\n"+
			"    #2307). This moves schema.golden and changes what REMOVING the attribute from a\n"+
			"    configuration means, so it wants an upgrade-guide entry.\n"+
			"  - read back through optionalString(configured, server), which consults what the\n"+
			"    configuration asked for and needs no schema change. Right where the attribute\n"+
			"    must stay Optional-only.\n\n"+
			"If \"\" and null are genuinely different states for the column behind it, add it to\n"+
			"legitimatelyNullable with a reason about the SERVER.",
			len(bad), strings.Join(bad, "\n  "))
	}
}

// TestTheLedgerDescribesTheTreeAsItIs is the other direction, and it is what
// stops legitimatelyNullable becoming a place excuses accumulate.
//
// An entry naming an attribute this gate no longer flags is stale: either the
// attribute was fixed, or it stopped being optional-only, or its name moved. In
// every one of those cases the entry now describes a tree that does not exist,
// and the next person reads it as a considered decision.
func TestTheLedgerDescribesTheTreeAsItIs(t *testing.T) {
	flagged := flaggedAttributes(t)

	for key, reason := range legitimatelyNullable {
		if _, still := flagged[key]; !still {
			t.Errorf("legitimatelyNullable has a stale entry for %q -- this gate no longer "+
				"flags it, so the entry describes a tree that is not there. Delete it.\n"+
				"  recorded reason: %s", key, reason)
		}
		if strings.TrimSpace(reason) == "" {
			t.Errorf("legitimatelyNullable[%q] has no reason. The reason is the whole "+
				"content of the entry: it has to say what \"\" means to the column behind "+
				"the attribute, which is the one thing this gate cannot read for itself", key)
		}
	}
}
