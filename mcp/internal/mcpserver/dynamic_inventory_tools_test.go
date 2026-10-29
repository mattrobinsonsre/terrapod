package mcpserver

import (
	"encoding/json"
	"go/ast"
	"go/parser"
	"go/token"
	"path/filepath"
	"strings"
	"testing"
)

// The two #2033 switches on the MCP surface.
//
// Both are per-workspace booleans permitting an ansible inventory PLUGIN on
// the runner — `dynamic_inventory_enabled` for a declared plugin config,
// `inventory_scripts_enabled` for an executable script — and they are
// deliberately independent, so an operator can take declared configs without
// also permitting arbitrary executables.
//
// They are settable from this server because it is NOT an authorization
// boundary: it carries the user's own token and the API applies the same RBAC
// it applies to the UI, the provider and curl, so withholding a property
// protects nobody and only lets an agent READ a setting it cannot SET. Risk is
// signalled by the `mutating` annotation, never by absence.
var dynamicInventoryFields = []string{
	"dynamic_inventory_enabled",
	"inventory_scripts_enabled",
}

// goFieldOf maps the json tag to the Go field it is written on, so the
// pass-through gate below can follow the link rather than merely find a
// mention.
var dynamicInventoryGoFields = map[string]string{
	"dynamic_inventory_enabled": "DynamicInventoryEnabled",
	"inventory_scripts_enabled": "InventoryScriptsEnabled",
}

// siblingOf is the OTHER switch — each description has to name it, because the
// independence of the two is the thing most likely to be misread as one
// setting. Derived rather than written out, so neither entry can go stale
// against the list above.
func siblingOf(field string) string {
	for _, other := range dynamicInventoryFields {
		if other != field {
			return other
		}
	}
	return ""
}

// TestTheDynamicInventorySwitchesReachTheSDKRequest is the gate the catalogue
// golden cannot be: the golden freezes each tool's INPUT SCHEMA, so a field
// declared on the input struct and never handed to the go-terrapod request
// would be advertised to an agent, accepted, serialised into the schema, and
// silently dropped — the mirror of a flag nothing reads. An agent would set the
// switch, get a 200 back, and the workspace would be unchanged.
//
// So this follows the link: every workspace request literal in crud.go must
// carry each switch as a key whose VALUE is the matching field off `in`. A key
// that exists is not the same as a key that is right, which is why the value is
// checked and not just the key's presence.
func TestTheDynamicInventorySwitchesReachTheSDKRequest(t *testing.T) {
	path := filepath.Join("crud.go")
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, path, nil, parser.ParseComments)
	if err != nil {
		t.Fatalf("parse %s: %v", path, err)
	}

	// Collect every terrapod.{Create,Update}WorkspaceRequest literal.
	wanted := map[string]bool{
		"CreateWorkspaceRequest": true,
		"UpdateWorkspaceRequest": true,
	}
	literals := map[string]*ast.CompositeLit{}
	ast.Inspect(file, func(n ast.Node) bool {
		lit, ok := n.(*ast.CompositeLit)
		if !ok {
			return true
		}
		sel, ok := lit.Type.(*ast.SelectorExpr)
		if !ok || !wanted[sel.Sel.Name] {
			return true
		}
		if _, dup := literals[sel.Sel.Name]; dup {
			t.Fatalf("more than one %s literal in %s; this gate assumes one per "+
				"tool and would otherwise check whichever it saw last", sel.Sel.Name, path)
		}
		literals[sel.Sel.Name] = lit
		return true
	})

	// A floor, so a rename that stops the matcher matching fails here rather
	// than passing on nothing.
	if len(literals) != len(wanted) {
		t.Fatalf("found %d workspace request literals in %s, want %d (%v) — if a tool "+
			"was renamed, teach this gate the new shape; do NOT drop the assertion",
			len(literals), path, len(wanted), literals)
	}

	for typeName, lit := range literals {
		for _, field := range dynamicInventoryFields {
			goField := dynamicInventoryGoFields[field]
			t.Run(typeName+"/"+goField, func(t *testing.T) {
				var value ast.Expr
				for _, elt := range lit.Elts {
					kv, ok := elt.(*ast.KeyValueExpr)
					if !ok {
						continue
					}
					key, ok := kv.Key.(*ast.Ident)
					if !ok || key.Name != goField {
						continue
					}
					value = kv.Value
				}
				if value == nil {
					t.Fatalf("%s does not set %s, so the %s field on the tool's input is "+
						"accepted and then dropped: an agent sets the switch, the call "+
						"succeeds, and the workspace is unchanged", typeName, goField, field)
				}
				// The value must be `in.<goField>` — not a literal, not a
				// different field. A `DynamicInventoryEnabled: in.InventoryScriptsEnabled`
				// transposition is exactly the shape a key-presence check misses.
				sel, ok := value.(*ast.SelectorExpr)
				if !ok {
					t.Fatalf("%s.%s is not read off the tool input at all", typeName, goField)
				}
				recv, ok := sel.X.(*ast.Ident)
				if !ok || recv.Name != "in" {
					t.Fatalf("%s.%s is not read off `in`", typeName, goField)
				}
				if sel.Sel.Name != goField {
					t.Fatalf("%s.%s is set from in.%s — the two switches are independent, so "+
						"a transposition silently applies the wrong one", typeName, goField, sel.Sel.Name)
				}
			})
		}
	}
}

// TestTheDynamicInventorySwitchesAreOfferedOnBothWorkspaceTools asserts the
// pair is on the create tool as well as the update one. The parity gate in the
// Python suite reads this file for the UPDATE surface only, so a create tool
// missing them would leave an agent having to create a workspace and then
// patch it — and nothing else would say so.
func TestTheDynamicInventorySwitchesAreOfferedOnBothWorkspaceTools(t *testing.T) {
	schemas := liveInputSchemasByName(t)
	for _, tool := range []string{"terrapod_workspace_create", "terrapod_workspace_update"} {
		raw, ok := schemas[tool]
		if !ok {
			t.Fatalf("tool %s is not registered", tool)
		}
		// `type` is a LIST for a pointer field (`["null","boolean"]`), so it
		// cannot be decoded as a string — one sibling on this same tool is a
		// `*[]string` and would break that. Kept raw and compared below.
		var doc struct {
			Properties map[string]struct {
				Type        json.RawMessage `json:"type"`
				Description string          `json:"description"`
			} `json:"properties"`
			Required []string `json:"required"`
		}
		if err := json.Unmarshal(raw, &doc); err != nil {
			t.Fatalf("decode %s input schema: %v", tool, err)
		}
		// The expected rendering is DERIVED from a sibling the catalogue has
		// already settled rather than written out: `plan_apply_enabled` is a
		// *bool on both tools, so whatever the SDK renders for one it must
		// render for these. A hardcoded `["null","boolean"]` would be a second
		// copy of the SDK's own convention, and would go stale with it.
		wantType := string(doc.Properties["plan_apply_enabled"].Type)
		if wantType == "" {
			t.Fatalf("%s has no plan_apply_enabled field; this gate derives the expected "+
				"pointer-boolean rendering from it, so teach it another *bool sibling", tool)
		}
		for _, field := range dynamicInventoryFields {
			prop, ok := doc.Properties[field]
			if !ok {
				t.Errorf("%s does not offer %s, so an agent can read the setting with "+
					"terrapod_workspace_get and not set it", tool, field)
				continue
			}
			if got := string(prop.Type); got != wantType {
				t.Errorf("%s.%s renders as %s, want %s (the same as the *bool sibling "+
					"plan_apply_enabled): it is one switch, not a parsed value, and unset "+
					"must stay distinguishable from false",
					tool, field, got, wantType)
			}
			for _, req := range doc.Required {
				if req == field {
					t.Errorf("%s.%s is REQUIRED; both switches default to off on the server "+
						"and omitting them must leave that alone", tool, field)
				}
			}
		}
	}
}

// TestTheDynamicInventoryDescriptionsSayWhereTheCodeRuns guards the four claims
// an agent cannot check for itself, on every tool that takes the pair.
//
// A field description IS frozen by the catalogue golden, so an unreviewed EDIT
// is caught there. What the golden cannot catch is an absent claim — it has no
// opinion about content — and these four are the ones whose absence changes
// what an agent does:
//
//   - the API never executes a plugin or a script in EITHER state, so turning a
//     switch on is not "let the API run arbitrary code";
//   - off means a plugin source is refused BY NAME, not resolved partially: a
//     short host set read as the truth is the failure the surface exists to
//     prevent, and an agent told only "off" would assume the latter;
//   - on means the API skips it and the RUNNER re-resolves and is
//     authoritative, so the API's own view is not the answer;
//   - the two switches are separate, so declared configs can be permitted
//     without executables.
func TestTheDynamicInventoryDescriptionsSayWhereTheCodeRuns(t *testing.T) {
	schemas := liveInputSchemasByName(t)
	for _, tool := range []string{"terrapod_workspace_create", "terrapod_workspace_update"} {
		for _, field := range dynamicInventoryFields {
			t.Run(tool+"/"+field, func(t *testing.T) {
				raw, ok := schemas[tool]
				if !ok {
					t.Fatalf("tool %s is not registered", tool)
				}
				var doc struct {
					Properties map[string]struct {
						Description string `json:"description"`
					} `json:"properties"`
				}
				if err := json.Unmarshal(raw, &doc); err != nil {
					t.Fatalf("decode %s input schema: %v", tool, err)
				}
				desc := doc.Properties[field].Description
				if desc == "" {
					t.Fatalf("%s.%s has no description", tool, field)
				}
				for _, want := range []string{
					// The API never runs one, in either state.
					"NEVER executes",
					"either state",
					// Off refuses the source outright rather than resolving
					// the rest and under-reporting the host set.
					"BY NAME",
					// On defers to the runner, which is the authority.
					"RUNNER",
					"authoritative",
					// The two are independent.
					siblingOf(field),
				} {
					if !strings.Contains(desc, want) {
						t.Errorf("%s.%s never says %q, so an agent cannot tell where the "+
							"code runs or what each state does:\n%s", tool, field, want, desc)
					}
				}
			})
		}
	}
}
