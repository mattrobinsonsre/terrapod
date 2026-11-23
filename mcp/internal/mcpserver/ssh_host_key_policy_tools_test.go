package mcpserver

import (
	"encoding/json"
	"go/ast"
	"go/parser"
	"go/token"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// `ssh_host_key_policy` (#2592) on the MCP workspace tools, where it replaced
// `ansible_host_key_checking` (#2318).
//
// Two things make this worth its own file rather than a line in the catalogue
// golden, and they are different halves of the surface:
//
//   - the golden freezes each tool's NAME, ANNOTATIONS and INPUT SCHEMA, so the
//     field and its description inside the schema are pinned by regenerating it.
//     What the golden does NOT freeze is the tool-level `Description`, nor
//     whether the field is handed to the SDK request at all -- a field declared
//     on the input struct and never passed through is advertised, accepted,
//     serialised into the schema and silently dropped. An agent would set the
//     policy, get a 200 back, and the workspace would be unchanged.
//   - the field is a value SET now rather than a boolean, and an agent cannot
//     sanity-check prose. Told the three values wrongly, it either declines to
//     act or sends something the server refuses; told that a host with no
//     approved key is unreachable, it will warn a user away from a configure
//     that would in fact have worked under `accept-new`. So the description has
//     to name all three and say what the middle one does.
//
// It is settable at all because this server is NOT an authorization boundary:
// it carries the user's own token and the API applies the same RBAC it applies
// to the UI, the provider and curl, so withholding the property protects nobody
// and only lets an agent READ a setting it cannot SET. Risk is signalled by the
// `mutating` annotation, never by absence.

const sshHostKeyPolicyField = "ssh_host_key_policy"

// sshHostKeyPolicyValues is the set the server accepts. Every description must
// name each one, because a description naming two of three is worse than one
// naming none: it reads as complete.
var sshHostKeyPolicyValues = []string{"strict", "accept-new", "off"}

var workspaceTools = []string{"terrapod_workspace_create", "terrapod_workspace_update"}

// namesValue reports whether desc names value as a WORD. `off` is a substring
// of `offered` and `offset`, so a bare Contains is satisfied by prose that
// never names the value -- which is how a description comes to be missing a
// third of the set while the gate reports it complete.
//
// Hyphens are word characters for this purpose: `accept-new` must match
// `accept-new`, and the boundary either side of the whole token is what matters.
func namesValue(desc, value string) bool {
	return regexp.MustCompile(`(^|[^\w-])` + regexp.QuoteMeta(value) + `([^\w-]|$)`).
		MatchString(desc)
}

// TestNamesValueMatchesWholeWordsOnly is the matcher's own self-test, with
// literal fixtures asserted independently of whatever the descriptions happen
// to contain.
//
// A tree mutation cannot isolate this clause: `off` occurs as a standalone word
// three times in each description, so weakening the matcher back to a bare
// `strings.Contains` leaves it satisfied by the other two occurrences and the
// mutation survives while telling you nothing. Fixtures pin the matcher
// directly, which is what makes a later narrowing fail here by name rather than
// quietly shrinking what the gate checks.
func TestNamesValueMatchesWholeWordsOnly(t *testing.T) {
	for _, tc := range []struct {
		desc, value string
		want        bool
	}{
		{"one of strict, accept-new or off; defaults to strict", "off", true},
		{"ends the sentence with off", "off", true},
		{"off starts the sentence", "off", true},
		// The whole point. A bare Contains accepts all three of these.
		{"a third value is offered", "off", false},
		{"an offset of three", "off", false},
		{"an off-by-one error", "off", false},
		// `accept-new` must match itself and not a longer token containing it.
		{"accept-new trusts on first contact", "accept-new", true},
		{"accept-newer is not a policy", "accept-new", false},
		// A hyphen is a word character here, on BOTH sides, and the asymmetry
		// that buys is deliberate: `--accept-new` does not count as naming the
		// value, and neither does `non-strict`. That is the price of excluding
		// `accept-newer`, and it is the right way round -- these descriptions
		// name the policy as a bare value, never as a flag, so the cost is
		// theoretical while `accept-newer` slipping through would not be.
		{"use --accept-new", "accept-new", false},
		{"non-strict is not a value", "strict", false},
		{"strictly speaking", "strict", false},
		{"strict verifies everything", "strict", true},
	} {
		t.Run(tc.value+"/"+tc.desc, func(t *testing.T) {
			if got := namesValue(tc.desc, tc.value); got != tc.want {
				t.Errorf("namesValue(%q, %q) = %v, want %v", tc.desc, tc.value, got, tc.want)
			}
		})
	}
}

func TestTheHostKeyPolicyIsOfferedOnBothWorkspaceTools(t *testing.T) {
	schemas := liveInputSchemasByName(t)
	for _, tool := range workspaceTools {
		t.Run(tool, func(t *testing.T) {
			raw, ok := schemas[tool]
			if !ok {
				t.Fatalf("%s is not registered", tool)
			}
			var schema struct {
				Properties map[string]struct {
					Description string `json:"description"`
				} `json:"properties"`
			}
			if err := json.Unmarshal(raw, &schema); err != nil {
				t.Fatalf("decode %s schema: %v", tool, err)
			}
			prop, ok := schema.Properties[sshHostKeyPolicyField]
			if !ok {
				t.Fatalf("%s does not offer %s, so an agent can read the policy with "+
					"terrapod_workspace_get and not change it — an asymmetry, not a control",
					tool, sshHostKeyPolicyField)
			}
			for _, v := range sshHostKeyPolicyValues {
				// Word-bounded, not a bare substring: `off` occurs inside
				// `offered`, `offset` and `off-by-one`, so a plain Contains
				// would be satisfied by a description that never names the
				// value at all. The two strictest values do not have that
				// problem and are bounded anyway, so the rule is uniform.
				if !namesValue(prop.Description, v) {
					t.Errorf("%s/%s: the description does not name %q as a value. An agent "+
						"cannot discover a value it is not told about, and the server "+
						"refuses anything outside the three", tool, sshHostKeyPolicyField, v)
				}
			}
			// The middle state is the whole reason the boolean was replaced, so
			// a description that lists it without saying what it DOES leaves an
			// agent to guess between "trust everything new" and "refuse
			// everything new" -- opposite actions.
			if !strings.Contains(prop.Description, "first") ||
				!strings.Contains(strings.ToLower(prop.Description), "change") {
				t.Errorf("%s/%s: the description names accept-new without saying it trusts "+
					"on FIRST contact and refuses a CHANGED key, which is the one thing an "+
					"agent needs to choose it over off", tool, sshHostKeyPolicyField)
			}

			// The retired boolean must be gone, not offered beside it: a tool
			// carrying both would advertise a field the server accepts on no
			// surface, and the catalogue would freeze it there.
			for name := range schema.Properties {
				if strings.Contains(name, "host_key_checking") {
					t.Errorf("%s still offers %q — #2592 replaced it, and the server "+
						"refuses a boolean with a 422", tool, name)
				}
			}
		})
	}
}

// TestTheHostKeyPolicyReachesTheSDKRequest follows the link the catalogue
// cannot: the field must be handed to the go-terrapod request, read off `in`,
// on BOTH request literals.
func TestTheHostKeyPolicyReachesTheSDKRequest(t *testing.T) {
	const goField = "SSHHostKeyPolicy"
	path := filepath.Join("crud.go")
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, path, nil, parser.ParseComments)
	if err != nil {
		t.Fatalf("parse %s: %v", path, err)
	}

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
			t.Fatalf("more than one %s literal in %s; this gate assumes one per tool",
				sel.Sel.Name, path)
		}
		literals[sel.Sel.Name] = lit
		return true
	})
	// A floor, so a rename that stops the matcher matching fails here rather
	// than passing on nothing.
	if len(literals) != len(wanted) {
		t.Fatalf("found %d workspace request literals in %s, want %d — teach this gate "+
			"the new shape; do NOT drop the assertion", len(literals), path, len(wanted))
	}

	for typeName, lit := range literals {
		t.Run(typeName, func(t *testing.T) {
			var value ast.Expr
			for _, elt := range lit.Elts {
				kv, ok := elt.(*ast.KeyValueExpr)
				if !ok {
					continue
				}
				if key, ok := kv.Key.(*ast.Ident); ok && key.Name == goField {
					value = kv.Value
				}
			}
			if value == nil {
				t.Fatalf("%s does not set %s, so the %s field on the tool's input is "+
					"accepted and then dropped: an agent sets the policy, the call "+
					"succeeds, and the workspace is unchanged",
					typeName, goField, sshHostKeyPolicyField)
			}
			sel, ok := value.(*ast.SelectorExpr)
			if !ok {
				t.Fatalf("%s.%s is not read off the tool input at all", typeName, goField)
			}
			if recv, ok := sel.X.(*ast.Ident); !ok || recv.Name != "in" {
				t.Fatalf("%s.%s is not read off `in`", typeName, goField)
			}
			if sel.Sel.Name != goField {
				t.Fatalf("%s.%s is set from in.%s — the wrong field would apply a "+
					"different setting silently", typeName, goField, sel.Sel.Name)
			}
		})
	}
}

// TestNoToolDescriptionClaimsAnUnpinnedHostIsUnreachable is the tool-level half,
// which NOTHING freezes.
//
// Three descriptions used to say that a host with no approved key cannot be
// reached "on a workspace with host-key checking on". That was true of the
// boolean and is false of the policy: under `accept-new` such a host IS reached,
// on first contact, which is precisely what that state exists for. The claim is
// the kind an agent acts on -- it would warn a user off a configure that works
// -- and no golden would ever have caught it, because the catalogue does not
// carry a tool's Description.
func TestNoToolDescriptionClaimsAnUnpinnedHostIsUnreachable(t *testing.T) {
	// Each retired phrasing, as a WHOLE claim rather than a fragment, so prose
	// that mentions the policy while stating the condition correctly survives.
	retired := []string{
		"with host-key checking on means a configure cannot reach",
		"with host-key checking on means the configure cannot reach",
		"with host-key checking on means a configure cannot reach that host",
	}
	for name, desc := range liveToolsByName(t) {
		for _, claim := range retired {
			if strings.Contains(desc, claim) {
				t.Errorf("%s says %q, which is false under ssh_host_key_policy "+
					"accept-new: the host is reached on first contact and refused "+
					"only once its key changes", name, claim)
			}
		}
		// And the retired attribute must not be named as something settable.
		// A historical mention is legitimate -- the create tool's own field
		// description says what it replaced -- so this looks for the shape of
		// an instruction rather than the bare name.
		for _, phrase := range []string{
			"set ansible_host_key_checking",
			"pass ansible_host_key_checking",
			"ansible_host_key_checking to false",
		} {
			if strings.Contains(strings.ToLower(desc), phrase) {
				t.Errorf("%s tells an agent to use %q, which the server refuses "+
					"everywhere since #2592", name, phrase)
			}
		}
	}
}
