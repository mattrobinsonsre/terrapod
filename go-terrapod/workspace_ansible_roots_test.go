package terrapod

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"
)

// The six ansible roots and run defaults a workspace carries (#1929):
// config-directory, inventory-paths, default-playbook, default-limit,
// inventory-include-platform and inventory-ignore-paths.
//
// The reflective gates in wire_completeness_test.go already prove each of the
// six reaches the wire and decodes. They cannot see the three properties below,
// each of which is a RELATIONSHIP between two values rather than the presence
// of one -- and each of which corrupts data rather than merely losing it:
//
//  1. `config-directory` has THREE states, not two. null means the workspace
//     has no configure operation; "" means the repository ROOT. A gate that
//     fills a field with one non-zero value cannot distinguish them, and a
//     consumer that collapsed them would give every terraform-only workspace a
//     configure operation rooted at the top of its repository.
//
//  2. `inventory-include-platform` DEFAULTS TO TRUE, so an absent key is not
//     false. `IsZero()` on a bool that defaults to true is false either way,
//     so the decode gate passes whether or not the line exists.
//
//  3. An empty non-nil slice must travel as `[]`, because that is the only
//     request that removes every inventory path. A nil slice must not travel
//     at all, or a PATCH overwrites a list the caller never mentioned.

// ── 1. config-directory: null, "" and a value are three different things ─────

func TestConfigDirectoryNullAndEmptyDecodeDifferently(t *testing.T) {
	// The whole reason Workspace.ConfigDirectory is a *string. If these two
	// ever read the same, the field has stopped carrying its meaning.
	cases := []struct {
		name string
		raw  string
		want *string // nil = no configure operation
	}{
		{"explicit null means no configure operation", `null`, nil},
		{"absent means no configure operation", ``, nil},
		{"empty string means the repository root", `""`, strPtr("")},
		{"a value means that subdirectory", `"ansible"`, strPtr("ansible")},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			attrs := map[string]json.RawMessage{"name": json.RawMessage(`"api"`)}
			if tc.raw != "" {
				attrs["config-directory"] = json.RawMessage(tc.raw)
			}
			ws := workspaceFromResource(&Resource{ID: "ws-1", Type: "workspaces", Attributes: attrs})

			switch {
			case tc.want == nil && ws.ConfigDirectory != nil:
				t.Fatalf("decoded %s as %q, want nil — a workspace with no "+
					"configure operation now claims one", tc.raw, *ws.ConfigDirectory)
			case tc.want != nil && ws.ConfigDirectory == nil:
				t.Fatalf("decoded %s as nil, want %q — the configure operation "+
					"was silently dropped", tc.raw, *tc.want)
			case tc.want != nil && *ws.ConfigDirectory != *tc.want:
				t.Fatalf("decoded %s as %q, want %q", tc.raw, *ws.ConfigDirectory, *tc.want)
			}
		})
	}
}

func TestCreateSendsAnEmptyConfigDirectoryRatherThanDroppingIt(t *testing.T) {
	// `&""` is the repository root, which is a real and ordinary value. An
	// emptiness guard here would make it impossible to express.
	attrs := workspaceCreateAttrs(CreateWorkspaceRequest{
		Name:            "api",
		ConfigDirectory: strPtr(""),
	})
	got, ok := attrs["config-directory"]
	if !ok {
		t.Fatal("create dropped config-directory=\"\" — there is no longer any " +
			"way to root a configure operation at the repository root")
	}
	if got != "" {
		t.Fatalf("create sent config-directory = %#v, want \"\"", got)
	}
}

func TestCreateOmitsConfigDirectoryWhenNil(t *testing.T) {
	attrs := workspaceCreateAttrs(CreateWorkspaceRequest{Name: "api"})
	if _, present := attrs["config-directory"]; present {
		t.Error("create sent config-directory when the caller set none — a " +
			"terraform-only workspace would be given a configure operation")
	}
}

func TestUpdateClearsConfigDirectoryWithAnExplicitNull(t *testing.T) {
	// Turning the configure operation OFF is a different request from rooting
	// it at "", so it has to reach the wire as JSON null. Asserted on the
	// marshalled bytes: a `nil` in the attrs map is only worth something if
	// MarshalResource actually emits null for it.
	attrs := workspaceUpdateAttrs(UpdateWorkspaceRequest{ClearConfigDirectory: true})
	got, ok := attrs["config-directory"]
	if !ok {
		t.Fatal("update dropped the clear — the configure operation cannot be turned off")
	}
	if got != nil {
		t.Fatalf("update sent config-directory = %#v, want nil", got)
	}

	body, err := MarshalResourceWithID("ws-1", "workspaces", attrs)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(body), `"config-directory":null`) {
		t.Errorf("marshalled body carries no explicit null: %s", body)
	}
}

func TestUpdateClearWinsOverAValue(t *testing.T) {
	// Both set is a caller bug, but it must resolve one way deterministically
	// and the documented way is that the clear wins.
	attrs := workspaceUpdateAttrs(UpdateWorkspaceRequest{
		ConfigDirectory:      strPtr("ansible"),
		ClearConfigDirectory: true,
	})
	if got := attrs["config-directory"]; got != nil {
		t.Errorf("clear did not win: config-directory = %#v, want nil", got)
	}
}

func TestUpdateOmitsConfigDirectoryWhenNeitherIsSet(t *testing.T) {
	attrs := workspaceUpdateAttrs(UpdateWorkspaceRequest{Name: "api"})
	if _, present := attrs["config-directory"]; present {
		t.Error("update sent config-directory when the caller mentioned neither " +
			"it nor the clear flag — PATCH would overwrite a stored root")
	}
}

// ── 2. inventory-include-platform defaults to TRUE ───────────────────────────

func TestIncludePlatformAbsentReadsAsTrueAndFalseReadsAsFalse(t *testing.T) {
	// Both halves matter. Absent-as-false would report the declared rows as
	// excluded on every workspace of a server predating #1929 (and on every
	// workspace read through /api/tfe/v2, which drops all six). An explicit
	// false read as true would hide the setting being switched off.
	absent := workspaceFromResource(&Resource{
		ID: "ws-1", Type: "workspaces",
		Attributes: map[string]json.RawMessage{"name": json.RawMessage(`"api"`)},
	})
	if !absent.InventoryIncludePlatform {
		t.Error("an absent inventory-include-platform read as false; the column " +
			"defaults to true, so this reports the declared rows as excluded")
	}

	off := workspaceFromResource(&Resource{
		ID: "ws-1", Type: "workspaces",
		Attributes: map[string]json.RawMessage{
			"name":                       json.RawMessage(`"api"`),
			"inventory-include-platform": json.RawMessage(`false`),
		},
	})
	if off.InventoryIncludePlatform {
		t.Error("an explicit false read as true — a workspace resolving its git " +
			"source alone would report its declared rows as contributing")
	}
}

func TestUpdateSendsIncludePlatformFalse(t *testing.T) {
	// The *bool exists for this case: a plain bool would drop false and
	// "resolve the git source alone" would be a silent no-op.
	off := false
	attrs := workspaceUpdateAttrs(UpdateWorkspaceRequest{InventoryIncludePlatform: &off})
	if got, ok := attrs["inventory-include-platform"]; !ok || got != false {
		t.Errorf("update dropped inventory-include-platform=false (got %v, present=%v)", got, ok)
	}
}

// ── 3. nil omits, empty non-nil clears ──────────────────────────────────────

func TestEmptySlicesTravelSoEveryPathCanBeRemoved(t *testing.T) {
	attrs := workspaceUpdateAttrs(UpdateWorkspaceRequest{
		InventoryPaths:       []string{},
		InventoryIgnorePaths: []string{},
	})
	for _, key := range []string{"inventory-paths", "inventory-ignore-paths"} {
		got, ok := attrs[key]
		if !ok {
			t.Errorf("update dropped an explicit empty %s — the one request that "+
				"removes every entry would silently do nothing", key)
			continue
		}
		if v, isSlice := got.([]string); !isSlice || len(v) != 0 {
			t.Errorf("update sent %s = %#v, want an empty []string", key, got)
		}
	}

	body, err := MarshalResourceWithID("ws-1", "workspaces", attrs)
	if err != nil {
		t.Fatal(err)
	}
	// A nil slice would marshal as `null`, which the server reads as absent,
	// so the clear would be lost at the JSON boundary rather than in the
	// builder. Assert the bytes, not just the map.
	if !strings.Contains(string(body), `"inventory-paths":[]`) {
		t.Errorf("empty inventory-paths did not marshal as []: %s", body)
	}
}

func TestNilSlicesAndPointersAreOmittedSoPatchLeavesThemAlone(t *testing.T) {
	attrs := workspaceUpdateAttrs(UpdateWorkspaceRequest{Name: "api"})
	for _, key := range []string{
		"inventory-paths", "inventory-ignore-paths",
		"default-playbook", "default-limit", "inventory-include-platform",
	} {
		if _, present := attrs[key]; present {
			t.Errorf("update sent %q when the caller set nothing — PATCH would "+
				"overwrite a value the caller never mentioned", key)
		}
	}
}

func TestUpdateCanClearThePlaybookAndLimitWithAnEmptyString(t *testing.T) {
	// *string, not string: "stop defaulting to a playbook" and "widen the run
	// back to the whole inventory" are both expressed by "".
	empty := ""
	attrs := workspaceUpdateAttrs(UpdateWorkspaceRequest{
		DefaultPlaybook: &empty,
		DefaultLimit:    &empty,
	})
	for _, key := range []string{"default-playbook", "default-limit"} {
		if got, ok := attrs[key]; !ok || got != "" {
			t.Errorf("update dropped %s=\"\" (got %#v, present=%v)", key, got, ok)
		}
	}
}

// ── Round trip through a server, happy path and error path ──────────────────

func TestCreateWorkspaceWithAnsibleRoots_Happy(t *testing.T) {
	f := newWorkspaceFixtureServer(t)
	f.createHandler = func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(minimalWorkspaceBody("ws-aaa", "configure-only", map[string]any{
			"config-directory":           "ansible",
			"inventory-paths":            []string{"inventory/production", "inventory/extra.yml"},
			"default-playbook":           "site.yml",
			"default-limit":              "web",
			"inventory-include-platform": false,
			"inventory-ignore-paths":     []string{"archive/"},
		})))
	}
	c := f.client()
	off := false
	ws, err := c.CreateWorkspace(t.Context(), CreateWorkspaceRequest{
		Name:                     "configure-only",
		ConfigDirectory:          strPtr("ansible"),
		InventoryPaths:           []string{"inventory/production", "inventory/extra.yml"},
		DefaultPlaybook:          "site.yml",
		DefaultLimit:             "web",
		InventoryIncludePlatform: &off,
		InventoryIgnorePaths:     []string{"archive/"},
	})
	if err != nil {
		t.Fatalf("CreateWorkspace: %v", err)
	}

	// Read side.
	if ws.ConfigDirectory == nil || *ws.ConfigDirectory != "ansible" {
		t.Errorf("ConfigDirectory = %v", ws.ConfigDirectory)
	}
	if len(ws.InventoryPaths) != 2 || ws.InventoryPaths[0] != "inventory/production" {
		t.Errorf("InventoryPaths = %v — order is precedence, so it must survive", ws.InventoryPaths)
	}
	if ws.DefaultPlaybook != "site.yml" || ws.DefaultLimit != "web" {
		t.Errorf("defaults: playbook=%q limit=%q", ws.DefaultPlaybook, ws.DefaultLimit)
	}
	if ws.InventoryIncludePlatform {
		t.Error("InventoryIncludePlatform decoded as true from an explicit false")
	}
	if len(ws.InventoryIgnorePaths) != 1 || ws.InventoryIgnorePaths[0] != "archive/" {
		t.Errorf("InventoryIgnorePaths = %v", ws.InventoryIgnorePaths)
	}

	// Write side: every one of the six is in the body the server received.
	var sent struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(f.lastBody, &sent); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{
		"config-directory", "inventory-paths", "default-playbook",
		"default-limit", "inventory-include-platform", "inventory-ignore-paths",
	} {
		if _, present := sent.Data.Attributes[key]; !present {
			t.Errorf("create body dropped %q: %s", key, f.lastBody)
		}
	}
}

func TestCreateWorkspaceWithAnsibleRoots_ServerRefusal(t *testing.T) {
	// The error path. A refusal has to surface as an error rather than as a
	// zero-valued workspace, or a caller reads "no configure operation" from a
	// request the server rejected.
	f := newWorkspaceFixtureServer(t)
	f.createHandler = func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(
			`{"errors":[{"status":"422","detail":"inventory-paths must be a list of strings"}]}`))
	}
	c := f.client()
	ws, err := c.CreateWorkspace(t.Context(), CreateWorkspaceRequest{
		Name:           "configure-only",
		InventoryPaths: []string{"inventory/production"},
	})
	if err == nil {
		t.Fatalf("expected an error, got workspace %+v", ws)
	}
	if !strings.Contains(err.Error(), "inventory-paths") {
		t.Errorf("error does not carry the server's reason: %v", err)
	}
}

func TestUpdateWorkspaceTurningTheConfigureOperationOff(t *testing.T) {
	f := newWorkspaceFixtureServer(t)
	f.updateHandler = func(w http.ResponseWriter, r *http.Request) {
		// The server echoes null, which is what "no configure operation" is.
		_, _ = w.Write([]byte(`{"data":{"id":"ws-aaa","type":"workspaces","attributes":{
		  "name":"api","config-directory":null}}}`))
	}
	c := f.client()
	ws, err := c.UpdateWorkspace(t.Context(), "ws-aaa", UpdateWorkspaceRequest{
		ClearConfigDirectory: true,
	})
	if err != nil {
		t.Fatalf("UpdateWorkspace: %v", err)
	}
	if ws.ConfigDirectory != nil {
		t.Errorf("ConfigDirectory = %q, want nil", *ws.ConfigDirectory)
	}
	if !strings.Contains(string(f.lastBody), `"config-directory":null`) {
		t.Errorf("request body carried no explicit null: %s", f.lastBody)
	}
}

// ── The helper the pointer read is built on ─────────────────────────────────

func TestGetStringPtrAttrSeparatesNullFromEmpty(t *testing.T) {
	res := &Resource{
		ID: "x", Type: "t",
		Attributes: map[string]json.RawMessage{
			"nulled": json.RawMessage(`null`),
			"empty":  json.RawMessage(`""`),
			"value":  json.RawMessage(`"v"`),
			"wrong":  json.RawMessage(`7`),
		},
	}
	if got := GetStringPtrAttr(res, "nulled"); got != nil {
		t.Errorf("null read as %q, want nil", *got)
	}
	if got := GetStringPtrAttr(res, "absent"); got != nil {
		t.Errorf("an absent key read as %q, want nil", *got)
	}
	if got := GetStringPtrAttr(res, "empty"); got == nil || *got != "" {
		t.Errorf(`"" read as %v, want a pointer to ""`, got)
	}
	if got := GetStringPtrAttr(res, "value"); got == nil || *got != "v" {
		t.Errorf(`"v" read as %v`, got)
	}
	// A type mismatch reads as nil rather than panicking, matching every other
	// getter in jsonapi.go.
	if got := GetStringPtrAttr(res, "wrong"); got != nil {
		t.Errorf("a non-string read as %q, want nil", *got)
	}
}

func strPtr(s string) *string { return &s }
