package terrapod

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

// Every workspace field that CAN cross the wire actually does.
//
// This is a gate, not a regression pin. `debug-mode` and `ai-policy-mode`
// shipped inert because the request builders are hand-rolled `map[string]any`
// and the `json` tags on the request structs are never consulted -- so adding
// a field to the struct does nothing until someone adds a line to the builder,
// and NOTHING FAILS when they don't. The fix for those two came with a test
// named `TestWorkspaceDecodesTheSettingsItSends`, which asserted exactly those
// two fields: a name claiming the class, covering two instances of it.
//
// It missed `plan-expiry-seconds`, which had the identical defect in the same
// struct -- written by both builders, served by the API, never decoded, so the
// provider's non-nil guard always took the null branch and
// `plan_expiry_seconds = 3600` failed the apply with "Provider produced
// inconsistent result after apply".
//
// So: enumerate, don't list. These derive the field set by reflection, which
// means a field added tomorrow is covered without anyone remembering to add it
// here. The exemptions are explicit and each says why.

// tagOf returns the kebab-case wire name from a `json:"..."` tag, or "".
func tagOf(f reflect.StructField) string {
	tag := f.Tag.Get("json")
	if tag == "" || tag == "-" {
		return ""
	}
	return strings.Split(tag, ",")[0]
}

// Fields a request struct carries that deliberately do NOT travel as a plain
// attribute, with the reason. An entry here is a decision; anything else that
// fails is a bug.
var notPlainAttributes = map[string]string{
	"name":              "create sends it as the resource's identity, update as a rename",
	"vcs-connection-id": "travels in the JSON:API relationships block, not attributes",
	"organization":      "single-org: the path carries it",
	// An alias, not a drop: both builders fold TerraformVersion into the
	// canonical `engine-version` when EngineVersion is unset (#1559), so the
	// value reaches the wire under the other name. Pinned by
	// TestTheTerraformVersionAliasReachesTheWire below, so this exemption
	// cannot quietly become a real drop.
	"terraform-version": "an alias the builders send as engine-version",
}

func wireNames(v any) map[string]bool {
	out := map[string]bool{}
	t := reflect.TypeOf(v)
	for i := 0; i < t.NumField(); i++ {
		if n := tagOf(t.Field(i)); n != "" {
			out[n] = true
		}
	}
	return out
}

// fill sets every exported field of a struct to a non-zero value, so no
// builder's emptiness guard can hide a dropped field. Reflective rather than a
// hand-written literal on purpose: a literal is the same "list, don't
// enumerate" mistake this gate exists to end.
func fill(v reflect.Value) {
	for i := 0; i < v.NumField(); i++ {
		f := v.Field(i)
		if !f.CanSet() {
			continue
		}
		switch f.Kind() {
		case reflect.Bool:
			f.SetBool(true)
		case reflect.String:
			f.SetString("x")
		case reflect.Int, reflect.Int64:
			f.SetInt(7)
		case reflect.Slice:
			f.Set(reflect.MakeSlice(f.Type(), 1, 1))
			fe := f.Index(0)
			if fe.Kind() == reflect.String {
				fe.SetString("x")
			}
		case reflect.Map:
			f.Set(reflect.MakeMap(f.Type()))
		case reflect.Pointer:
			p := reflect.New(f.Type().Elem())
			switch p.Elem().Kind() {
			case reflect.Bool:
				p.Elem().SetBool(true)
			case reflect.String:
				p.Elem().SetString("x")
			case reflect.Int, reflect.Int64:
				p.Elem().SetInt(7)
			}
			f.Set(p)
		}
	}
}

func TestEveryCreateRequestFieldReachesTheWire(t *testing.T) {
	req := CreateWorkspaceRequest{}
	fill(reflect.ValueOf(&req).Elem())
	assertAllSent(t, "CreateWorkspaceRequest", req, workspaceCreateAttrs(req), "workspaceCreateAttrs")
}

func TestEveryUpdateRequestFieldReachesTheWire(t *testing.T) {
	req := UpdateWorkspaceRequest{}
	fill(reflect.ValueOf(&req).Elem())
	assertAllSent(t, "UpdateWorkspaceRequest", req, workspaceUpdateAttrs(req), "workspaceUpdateAttrs")
}

func assertAllSent(t *testing.T, structName string, req any, attrs map[string]any, builder string) {
	t.Helper()
	for name := range wireNames(req) {
		if _, excused := notPlainAttributes[name]; excused {
			continue
		}
		if _, sent := attrs[name]; !sent {
			t.Errorf("%s declares %q and %s drops it, so it never reaches the "+
				"wire. Add a line to the builder, or record it in notPlainAttributes "+
				"with the reason it does not travel as a plain attribute.",
				structName, name, builder)
		}
	}
}

func TestEveryWorkspaceFieldIsDecoded(t *testing.T) {
	// A field the SDK can send but cannot read back is silent drift: the
	// provider's Read leaves prior state in place and `plan` shows nothing.
	skip := map[string]string{
		"id": "the resource id, not an attribute",
		// Decoded from the JSON:API relationships block by GetRelationshipID,
		// not from attributes -- so this harness, which only synthesises
		// attributes, cannot see it. Verified present at workspaceFromResource.
		"vcs-connection-id": "read from the vcs-connection relationship",
	}

	// Build a resource carrying every attribute name the struct declares, with
	// a type-appropriate non-zero value, then require the decoder to surface it.
	ws := Workspace{}
	t2 := reflect.TypeOf(ws)
	attrs := map[string]any{}
	for i := 0; i < t2.NumField(); i++ {
		f := t2.Field(i)
		name := tagOf(f)
		if name == "" {
			continue
		}
		switch f.Type.Kind() {
		case reflect.Bool:
			attrs[name] = true
		case reflect.String:
			attrs[name] = "x"
		case reflect.Int64, reflect.Int:
			attrs[name] = 7
		case reflect.Pointer:
			attrs[name] = 7
		}
	}
	res := resourceWithAttrs("ws-1", "workspaces", attrs)
	got := workspaceFromResource(res)

	gv := reflect.ValueOf(*got)
	for i := 0; i < t2.NumField(); i++ {
		f := t2.Field(i)
		name := tagOf(f)
		if name == "" {
			continue
		}
		if _, ok := skip[name]; ok {
			continue
		}
		if _, supplied := attrs[name]; !supplied {
			continue // a type this harness does not synthesise
		}
		if gv.Field(i).IsZero() {
			t.Errorf("Workspace.%s (%q) is never decoded: the server may send it and "+
				"every SDK consumer reads the zero value. Add it to workspaceFromResource.",
				f.Name, name)
		}
	}
}

// resourceWithAttrs builds a Resource whose Attributes are json.RawMessage,
// which is what the decoders read.
func resourceWithAttrs(id, typ string, attrs map[string]any) *Resource {
	raw := map[string]json.RawMessage{}
	for k, v := range attrs {
		b, err := json.Marshal(v)
		if err != nil {
			panic(err)
		}
		raw[k] = b
	}
	return &Resource{ID: id, Type: typ, Attributes: raw}
}

// The alias the exemption above depends on. Without this, recording
// `terraform-version` in notPlainAttributes would silence the gate for a field
// that might genuinely stop being sent -- which is the "grow the exemption list
// to make it green" failure the gate exists to prevent.
func TestTheTerraformVersionAliasReachesTheWire(t *testing.T) {
	for _, tc := range []struct {
		name  string
		attrs map[string]any
	}{
		{"create", workspaceCreateAttrs(CreateWorkspaceRequest{TerraformVersion: "1.9.2"})},
		{"update", workspaceUpdateAttrs(UpdateWorkspaceRequest{TerraformVersion: "1.9.2"})},
	} {
		if got := tc.attrs["engine-version"]; got != "1.9.2" {
			t.Errorf("%s: TerraformVersion must be sent as engine-version, got %v", tc.name, got)
		}
		if _, sent := tc.attrs["terraform-version"]; sent {
			t.Errorf("%s: sent terraform-version as its own attribute; the server reads engine-version", tc.name)
		}
	}
}

// EngineVersion wins when both are set -- otherwise a caller migrating to the
// new name would silently keep sending the old value.
func TestEngineVersionBeatsTheAlias(t *testing.T) {
	attrs := workspaceCreateAttrs(CreateWorkspaceRequest{EngineVersion: "1.9.2", TerraformVersion: "1.5.0"})
	if got := attrs["engine-version"]; got != "1.9.2" {
		t.Errorf("EngineVersion must win over TerraformVersion, got %v", got)
	}
}
