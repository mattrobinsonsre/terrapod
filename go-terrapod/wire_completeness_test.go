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
	"name": "create sends it as the resource's identity, update as a rename",
	// CREATE only. On UPDATE it travels as a plain attribute as well, because
	// that is the only one of the two that can carry a CLEAR -- see
	// notPlainAttributesUpdate, which drops this entry so the gate REQUIRES it.
	"vcs-connection-id": "create sends it in the JSON:API relationships block, not attributes",
	"organization":      "single-org: the path carries it",
	// An alias, not a drop: both builders fold TerraformVersion into the
	// canonical `engine-version` when EngineVersion is unset (#1559), so the
	// value reaches the wire under the other name. Pinned by
	// TestTheTerraformVersionAliasReachesTheWire below, so this exemption
	// cannot quietly become a real drop.
	"terraform-version": "an alias the builders send as engine-version",
}

// notPlainAttributesUpdate is notPlainAttributes minus `vcs-connection-id`,
// which on UPDATE does travel as a plain attribute (#2310): a nil pointer means
// "leave unchanged" and a pointer to "" means "clear", and only the attribute
// can express the second -- an ABSENT relationship is indistinguishable from
// leave-unchanged. So the update gate requires it rather than excusing it.
//
// Derived from the base map rather than written out, so an exemption added there
// is inherited here instead of silently applying to create alone.
var notPlainAttributesUpdate = func() map[string]string {
	m := map[string]string{}
	for k, v := range notPlainAttributes {
		if k == "vcs-connection-id" {
			continue
		}
		m[k] = v
	}
	return m
}()

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
	assertAllSent(t, "CreateWorkspaceRequest", req, workspaceCreateAttrs(req), "workspaceCreateAttrs", notPlainAttributes)
}

func TestEveryUpdateRequestFieldReachesTheWire(t *testing.T) {
	req := UpdateWorkspaceRequest{}
	fill(reflect.ValueOf(&req).Elem())
	assertAllSent(t, "UpdateWorkspaceRequest", req, workspaceUpdateAttrs(req), "workspaceUpdateAttrs", notPlainAttributesUpdate)
}

func assertAllSent(
	t *testing.T, structName string, req any, attrs map[string]any, builder string,
	excusedSet map[string]string,
) {
	t.Helper()
	for name := range wireNames(req) {
		if _, excused := excusedSet[name]; excused {
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
			// Synthesise by the pointed-to kind, not a bare 7. Every pointer
			// field on Workspace was an *int64 until `config-directory`
			// (#1929) arrived as a *string, and `7` cannot be unmarshalled
			// into one -- so the decoder returned nil, the field read as zero,
			// and this gate reported a field it DOES decode as undecoded.
			// A harness that only synthesises the types it has already seen
			// fails on the next one, in the direction that looks like a bug in
			// the code rather than in the harness.
			switch f.Type.Elem().Kind() {
			case reflect.String:
				attrs[name] = "x"
			case reflect.Bool:
				attrs[name] = true
			default:
				attrs[name] = 7
			}
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

// Every *string field on the update request sends an explicit "" — the question
// the gate above cannot ask.
//
// `TestEveryUpdateRequestFieldReachesTheWire` fills every field with a non-zero
// value, so it answers "does this field cross the wire AT ALL". Four fields
// crossed it for "x" and were silently dropped for "", because they were plain
// strings with `,omitempty`: the server then kept its stored value and returned
// it, and the apply failed "Provider produced inconsistent result after apply"
// (#2310). Presence and value-fidelity are different properties, and the second
// is the one an operator meets when clearing a field.
//
// Derived by reflection over the pointer-to-string fields, so a field added
// tomorrow is covered without anyone remembering this file.
func TestEveryUpdatePointerStringFieldSendsAnExplicitEmptyString(t *testing.T) {
	req := UpdateWorkspaceRequest{}
	v := reflect.ValueOf(&req).Elem()
	rt := v.Type()

	want := map[string]string{} // wire name -> Go field name
	for i := 0; i < rt.NumField(); i++ {
		f := rt.Field(i)
		if f.Type.Kind() != reflect.Pointer || f.Type.Elem().Kind() != reflect.String {
			continue
		}
		empty := ""
		v.Field(i).Set(reflect.ValueOf(&empty))
		name := tagOf(f)
		if name == "" {
			// `json:"-"` fields still travel, by another route. vcs-connection-id
			// is emitted as an attribute precisely so a clear can be expressed.
			if f.Name == "VCSConnectionID" {
				name = "vcs-connection-id"
			} else {
				continue
			}
		}
		want[name] = f.Name
	}
	// A floor alone is not enough: the derived set loses a field the moment it
	// stops being a pointer, and other *string fields keep the count up — so a
	// regression on one of these four would shrink the subject silently while
	// still passing a `len >= 4` check. Mutation-proven: reverting
	// `VCSRepoURL` to a plain string left the count at four and only the
	// exported-surface golden noticed. So assert MEMBERSHIP by name as well.
	for _, required := range []string{
		"ansible-execution-environment", "vcs-branch", "vcs-connection-id", "vcs-repo-url",
	} {
		if _, ok := want[required]; !ok {
			t.Errorf("%q is no longer a *string on UpdateWorkspaceRequest, so an "+
				"explicit \"\" cannot be distinguished from an omitted one and this "+
				"gate silently stopped covering it (#2310)", required)
		}
	}
	if len(want) < 4 {
		t.Fatalf("found only %d pointer-to-string fields on UpdateWorkspaceRequest; "+
			"this gate derives its subject, so too few means the derivation broke "+
			"rather than that the struct is small", len(want))
	}

	attrs := workspaceUpdateAttrs(req)
	for name, goField := range want {
		got, sent := attrs[name]
		if !sent {
			t.Errorf("%s is a *string set to \"\" and %q never reaches the wire. "+
				"An explicit empty must be sent, or the server keeps its stored "+
				"value and the apply fails as an inconsistent result.", goField, name)
			continue
		}
		if got != "" {
			t.Errorf("%s sent %q as %#v, want an empty string", goField, name, got)
		}
	}
}

// And the other direction, which is what stops the careless fix. Sending
// unconditionally would make every PATCH clear every field the caller did not
// mention — so a nil pointer must send NOTHING.
func TestANilPointerSendsNothing(t *testing.T) {
	attrs := workspaceUpdateAttrs(UpdateWorkspaceRequest{})
	rt := reflect.TypeOf(UpdateWorkspaceRequest{})
	checked := 0
	for i := 0; i < rt.NumField(); i++ {
		f := rt.Field(i)
		if f.Type.Kind() != reflect.Pointer || f.Type.Elem().Kind() != reflect.String {
			continue
		}
		name := tagOf(f)
		if name == "" && f.Name == "VCSConnectionID" {
			name = "vcs-connection-id"
		}
		if name == "" {
			continue
		}
		checked++
		if _, sent := attrs[name]; sent {
			t.Errorf("%s is nil and %q was still sent; an unmentioned field must "+
				"not be cleared", f.Name, name)
		}
	}
	if checked < 4 {
		t.Fatalf("checked only %d fields; the derivation broke", checked)
	}
}

// A relationship cannot express a clear, so the pointer semantics land on the
// attribute and workspaceRels stays quiet for both nil and "".
func TestWorkspaceRelsDistinguishesLeaveUnchangedFromClear(t *testing.T) {
	if got := workspaceRels(nil); got != nil {
		t.Errorf("nil (leave unchanged) produced a relationship: %#v", got)
	}
	empty := ""
	if got := workspaceRels(&empty); got != nil {
		t.Errorf("a clear must ride on the attribute, not a relationship: %#v", got)
	}
	id := "vcs-123"
	got := workspaceRels(&id)
	if got == nil {
		t.Fatal("a concrete id produced no relationship")
	}
	rel, ok := got["vcs-connection"].(map[string]any)
	if !ok {
		t.Fatalf("unexpected shape: %#v", got)
	}
	data, ok := rel["data"].(map[string]any)
	if !ok || data["id"] != id || data["type"] != "vcs-connections" {
		t.Errorf("relationship data is %#v, want id=%q type=vcs-connections", rel["data"], id)
	}
}
