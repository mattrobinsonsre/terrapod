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

// The #2538 cron-schedule settings on the MCP surface.
//
// A configure starts three ways: a push to config_directory
// (auto_configure_enabled), an apply (configure_after_apply), and a cron
// SCHEDULE -- these three. They shipped on terrapod_workspace_update and were
// absent from terrapod_workspace_create, so an agent had to create a workspace
// and then patch it while the server, go-terrapod, the Terraform provider and
// the web create page all took them at creation.
//
// They belong on both tools because this server is NOT an authorization
// boundary: it carries the user's own token and the API applies the same RBAC
// it applies to the UI, the provider and curl. Withholding a property protects
// nobody and only leaves an agent able to READ a setting it cannot SET. Risk
// is signalled by the `mutating` annotation, never by absence.
var scheduledConfigureFields = []string{
	"scheduled_configure_enabled",
	"scheduled_configure_cron",
	"scheduled_configure_timezone",
}

// scheduledConfigureGoFields maps each json tag to the Go field it is written
// on, so the pass-through gate below can follow the link rather than merely
// find a mention.
var scheduledConfigureGoFields = map[string]string{
	"scheduled_configure_enabled":  "ScheduledConfigureEnabled",
	"scheduled_configure_cron":     "ScheduledConfigureCron",
	"scheduled_configure_timezone": "ScheduledConfigureTimezone",
}

// TestTheScheduledConfigureSettingsReachTheSDKRequest is the gate the catalogue
// golden cannot be: the golden freezes each tool's INPUT SCHEMA, so a field
// declared on the input struct and never handed to the go-terrapod request
// would be advertised to an agent, accepted, serialised into the schema, and
// silently dropped -- the mirror of a flag nothing reads. An agent would set
// the schedule, get a 200 back, and the workspace would never configure.
//
// So this follows the link: every workspace request literal in crud.go must
// carry each setting as a key whose VALUE is the matching field off `in`. A key
// that exists is not the same as a key that is right, which is why the value is
// checked and not just the key's presence -- a
// `ScheduledConfigureCron: in.ScheduledConfigureTimezone` transposition is
// exactly the shape a key-presence check misses, and it would store a zone
// where a cron expression belongs.
func TestTheScheduledConfigureSettingsReachTheSDKRequest(t *testing.T) {
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
			t.Fatalf("more than one %s literal in %s; this gate assumes one per "+
				"tool and would otherwise check whichever it saw last", sel.Sel.Name, path)
		}
		literals[sel.Sel.Name] = lit
		return true
	})

	// A floor, so a rename that stops the matcher matching fails here rather
	// than passing on nothing.
	if len(literals) != len(wanted) {
		t.Fatalf("found %d workspace request literals in %s, want %d (%v) -- if a tool "+
			"was renamed, teach this gate the new shape; do NOT drop the assertion",
			len(literals), path, len(wanted), literals)
	}

	for typeName, lit := range literals {
		for _, field := range scheduledConfigureFields {
			goField := scheduledConfigureGoFields[field]
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
						"accepted and then dropped: an agent sets the schedule, the call "+
						"succeeds, and the workspace never configures", typeName, goField, field)
				}
				sel, ok := value.(*ast.SelectorExpr)
				if !ok {
					t.Fatalf("%s.%s is not read off the tool input at all", typeName, goField)
				}
				recv, ok := sel.X.(*ast.Ident)
				if !ok || recv.Name != "in" {
					t.Fatalf("%s.%s is not read off `in`", typeName, goField)
				}
				if sel.Sel.Name != goField {
					t.Fatalf("%s.%s is set from in.%s -- a transposition stores one of the "+
						"three where another belongs, which nothing downstream can detect",
						typeName, goField, sel.Sel.Name)
				}
			})
		}
	}
}

// TestTheScheduledConfigureSettingsAreOfferedOnBothWorkspaceTools asserts the
// three are on the create tool as well as the update one. The parity gate in
// the Python suite reads this file for the UPDATE surface only, so a create
// tool missing them leaves an agent having to create a workspace and then
// patch it -- and nothing else would say so. That is the defect this test was
// written for.
//
// The rendered TYPE is checked too, and the create/update asymmetry is
// deliberate rather than an oversight: on CREATE there is no stored value, so
// "" and omitted mean the same thing for the two strings and a plain string is
// the honest type; on UPDATE the cron is nullable because an explicit "" is
// what REMOVES a stored schedule. Both expectations are DERIVED from siblings
// the catalogue has already settled, so neither is a second copy of the SDK's
// own rendering convention.
func TestTheScheduledConfigureSettingsAreOfferedOnBothWorkspaceTools(t *testing.T) {
	schemas := liveInputSchemasByName(t)

	// Per tool: the sibling whose rendering each field must match. The bool is
	// a pointer on both tools; the two strings are plain on create (like
	// config_directory, an ordinary path whose "" is the server default) and
	// nullable on update (like slack_channel, whose "" clears).
	siblings := map[string]map[string]string{
		"terrapod_workspace_create": {
			"scheduled_configure_enabled":  "plan_apply_enabled",
			"scheduled_configure_cron":     "config_directory",
			"scheduled_configure_timezone": "config_directory",
		},
		"terrapod_workspace_update": {
			"scheduled_configure_enabled":  "plan_apply_enabled",
			"scheduled_configure_cron":     "slack_channel",
			"scheduled_configure_timezone": "slack_channel",
		},
	}

	for tool, wantFrom := range siblings {
		raw, ok := schemas[tool]
		if !ok {
			t.Fatalf("tool %s is not registered", tool)
		}
		// `type` is a LIST for a pointer field (`["null","boolean"]`) and a
		// bare string otherwise, so it cannot be decoded as a string. Kept raw
		// and compared below.
		var doc struct {
			Properties map[string]struct {
				Type json.RawMessage `json:"type"`
			} `json:"properties"`
			Required []string `json:"required"`
		}
		if err := json.Unmarshal(raw, &doc); err != nil {
			t.Fatalf("decode %s input schema: %v", tool, err)
		}
		for _, field := range scheduledConfigureFields {
			prop, ok := doc.Properties[field]
			if !ok {
				t.Errorf("%s does not offer %s, so an agent can read the setting with "+
					"terrapod_workspace_get and not set it -- on create that means "+
					"creating the workspace and then patching it", tool, field)
				continue
			}
			sibling := wantFrom[field]
			wantType := string(doc.Properties[sibling].Type)
			if wantType == "" {
				t.Fatalf("%s has no %s field; this gate derives the expected rendering "+
					"from it, so teach it another sibling of the same shape", tool, sibling)
			}
			if got := string(prop.Type); got != wantType {
				t.Errorf("%s.%s renders as %s, want %s (the same as its sibling %s): on "+
					"create \"\" and omitted mean the same thing, and on update an explicit "+
					"\"\" is what clears the stored value", tool, field, got, wantType, sibling)
			}
			for _, req := range doc.Required {
				if req == field {
					t.Errorf("%s.%s is REQUIRED; the schedule is off by default on the "+
						"server and omitting these must leave that alone", tool, field)
				}
			}
		}
	}
}

// TestTheScheduledConfigureDescriptionsSayWhatAnAgentCannotCheck guards the
// claims whose ABSENCE changes what an agent does, on every tool that takes
// the three.
//
// A field description IS frozen by the catalogue golden, so an unreviewed EDIT
// is caught there. What the golden cannot catch is an absent claim -- it has no
// opinion about content -- and these are the ones an agent cannot work out for
// itself:
//
//   - the schedule is NOT auto_configure_enabled, which is the confusion the
//     two settings most invite: one fires on a push, this one on the calendar;
//   - the cron is FIVE fields and is refused (422) on six or on a period under
//     300 seconds, so an agent writing a seconds-resolution expression learns
//     why it is rejected before it tries;
//   - the zone is what makes it a schedule rather than an interval, which is
//     the whole reason the timezone field exists.
func TestTheScheduledConfigureDescriptionsSayWhatAnAgentCannotCheck(t *testing.T) {
	// Keyed on the field, because the three carry different claims.
	claims := map[string][]string{
		"scheduled_configure_enabled": {
			// Not the push trigger.
			"auto_configure_enabled",
			"calendar",
			// Needs a cron to do anything.
			"scheduled_configure_cron",
		},
		"scheduled_configure_cron": {
			// Five fields, not six.
			"FIVE-FIELD",
			"422",
			"300 seconds",
			// Stored verbatim, so a round trip does not reformat it.
			"never reformatted",
		},
		"scheduled_configure_timezone": {
			"IANA",
			// A schedule, not an interval -- the reason the field exists.
			"interval",
		},
	}

	schemas := liveInputSchemasByName(t)
	for _, tool := range []string{"terrapod_workspace_create", "terrapod_workspace_update"} {
		for _, field := range scheduledConfigureFields {
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
				for _, want := range claims[field] {
					if !strings.Contains(desc, want) {
						t.Errorf("%s.%s never says %q, so an agent cannot tell the schedule "+
							"apart from the other configure triggers or predict what the "+
							"server refuses:\n%s", tool, field, want, desc)
					}
				}
			})
		}
	}
}
