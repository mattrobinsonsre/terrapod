package terrapod

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"
)

// The two operation declarations a workspace carries (#1966) and the six
// ansible roots and run defaults beside them (#1929): plan-apply-enabled,
// configure-enabled, config-directory, inventory-paths, default-playbook,
// default-limit, inventory-include-platform and inventory-ignore-paths.
//
// The reflective gates in wire_completeness_test.go already prove each of the
// eight reaches the wire and decodes. They cannot see the three properties
// below, each of which is a RELATIONSHIP between two values rather than the
// presence of one -- and each of which corrupts data rather than merely
// losing it:
//
//  1. `plan-apply-enabled` DEFAULTS TO TRUE, and on create and update it is a
//     *bool for that reason. A plain bool's false is indistinguishable from
//     unset, so every caller that left the field alone would disable
//     plan/apply -- silently, and on the workspaces least expecting it. The
//     read struct carries plain bools with no `omitempty`, so a false survives
//     a round trip.
//
//     `config-directory` used to head this list with three states, null
//     meaning "no configure operation". It has two now: "" is the
//     configuration version's root and anything else is a subdirectory. The
//     declaration moved to `configure-enabled`, so the field is an ordinary
//     path -- which is why the create request takes a plain string while the
//     update request still takes a pointer, to tell "leave it alone" from "set
//     it to the root".
//
//  2. `inventory-include-platform` DEFAULTS TO TRUE, so an absent key is not
//     false. `IsZero()` on a bool that defaults to true is false either way,
//     so the decode gate passes whether or not the line exists.
//
//  3. An empty non-nil slice must travel as `[]`, because that is the only
//     request that removes every inventory path. A nil slice must not travel
//     at all, or a PATCH overwrites a list the caller never mentioned.

// ── 1. the operation declarations, and config-directory as a plain path ──────

func TestPlanApplyEnabledDefaultsTrueAndSurvivesAFalse(t *testing.T) {
	// The field whose default is TRUE, which is what makes it the dangerous
	// one: a decoder that lost a false would silently re-enable plan/apply on
	// a configure-only workspace, and a read struct with `omitempty` would
	// drop it on the way back out.
	cases := []struct {
		name     string
		raw      string
		wantPlan bool
		wantCfg  bool
	}{
		{"both absent", ``, false, false},
		{"plan/apply on, configure off", `true`, true, false},
		{"plan/apply off", `false`, false, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			attrs := map[string]json.RawMessage{"name": json.RawMessage(`"api"`)}
			if tc.raw != "" {
				attrs["plan-apply-enabled"] = json.RawMessage(tc.raw)
			}
			ws := workspaceFromResource(&Resource{ID: "ws-1", Type: "workspaces", Attributes: attrs})
			if ws.PlanApplyEnabled != tc.wantPlan {
				t.Errorf("PlanApplyEnabled = %v, want %v", ws.PlanApplyEnabled, tc.wantPlan)
			}
			if ws.ConfigureEnabled != tc.wantCfg {
				t.Errorf("ConfigureEnabled = %v, want %v", ws.ConfigureEnabled, tc.wantCfg)
			}
		})
	}

	// And a false must survive being marshalled back, which is the half
	// `omitempty` would break. Asserted on the bytes, because a struct field
	// reading false proves nothing about what leaves the process.
	body, err := json.Marshal(Workspace{Name: "api", PlanApplyEnabled: false, ConfigureEnabled: true})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(body), `"plan-apply-enabled":false`) {
		t.Errorf("a false plan-apply-enabled did not survive marshalling: %s", body)
	}
}

func TestCreateDistinguishesAnUnsetBoolFromAnExplicitFalse(t *testing.T) {
	// The whole reason the create request takes *bool. Omitted means "take the
	// server default"; &false means "off". A plain bool could only say one of
	// them, and would say the wrong one.
	t.Run("omitted sends neither key", func(t *testing.T) {
		attrs := workspaceCreateAttrs(CreateWorkspaceRequest{Name: "api"})
		for _, k := range []string{"plan-apply-enabled", "configure-enabled"} {
			if _, present := attrs[k]; present {
				t.Errorf("create sent %s when the caller set none — the server "+
					"default is what an unset field means", k)
			}
		}
	})
	t.Run("an explicit false is sent", func(t *testing.T) {
		attrs := workspaceCreateAttrs(CreateWorkspaceRequest{
			Name:             "api",
			PlanApplyEnabled: boolPtr(false),
			ConfigureEnabled: boolPtr(true),
		})
		if got, ok := attrs["plan-apply-enabled"]; !ok || got != false {
			t.Errorf("create sent plan-apply-enabled = %#v (present %v), want false", got, ok)
		}
		if got, ok := attrs["configure-enabled"]; !ok || got != true {
			t.Errorf("create sent configure-enabled = %#v (present %v), want true", got, ok)
		}
	})
}

func TestUpdateOmitsABoolTheCallerDidNotMention(t *testing.T) {
	// A plain bool here would flip the field to false on every PATCH that did
	// not mention it — the trap AutoApply's own comment records.
	attrs := workspaceUpdateAttrs(UpdateWorkspaceRequest{Name: "api"})
	for _, k := range []string{"plan-apply-enabled", "configure-enabled"} {
		if _, present := attrs[k]; present {
			t.Errorf("update sent %s when the caller never mentioned it — a PATCH "+
				"would turn an operation off behind the caller's back", k)
		}
	}
}

func TestConfigDirectoryDecodesAsAPlainPath(t *testing.T) {
	// Two states, not three (#1966). An absent or null key and "" all mean the
	// configuration version's root, because the field is a path and the
	// question it used to answer — does this workspace have a configure
	// operation — is `configure-enabled` now.
	cases := []struct {
		name string
		raw  string
		want string
	}{
		{"absent means the configuration version's root", ``, ""},
		{"explicit null decodes as the root rather than erroring", `null`, ""},
		{"empty string means the root", `""`, ""},
		{"a value means that subdirectory", `"ansible"`, "ansible"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			attrs := map[string]json.RawMessage{"name": json.RawMessage(`"api"`)}
			if tc.raw != "" {
				attrs["config-directory"] = json.RawMessage(tc.raw)
			}
			ws := workspaceFromResource(&Resource{ID: "ws-1", Type: "workspaces", Attributes: attrs})
			if ws.ConfigDirectory != tc.want {
				t.Errorf("decoded %s as %q, want %q", tc.raw, ws.ConfigDirectory, tc.want)
			}
		})
	}
}

func TestCreateOmitsAnEmptyConfigDirectory(t *testing.T) {
	// Safe to omit precisely because "" and absent now mean the same thing:
	// the configuration version's root, which is also the column default. That
	// is what the three-state pointer existed to prevent, and what removing
	// the third state buys.
	attrs := workspaceCreateAttrs(CreateWorkspaceRequest{Name: "api"})
	if _, present := attrs["config-directory"]; present {
		t.Error("create sent config-directory when the caller set none")
	}
	withValue := workspaceCreateAttrs(CreateWorkspaceRequest{Name: "api", ConfigDirectory: "ansible"})
	if got := withValue["config-directory"]; got != "ansible" {
		t.Errorf("create sent config-directory = %#v, want \"ansible\"", got)
	}
}

func TestUpdateCanRootTheOperationAtTheConfigurationRoot(t *testing.T) {
	// Why the UPDATE request keeps a pointer where create does not: a PATCH
	// has to tell "leave the stored value alone" from "set it to the root",
	// and on a workspace whose stored root is `ansible` those are different
	// requests.
	attrs := workspaceUpdateAttrs(UpdateWorkspaceRequest{ConfigDirectory: strPtr("")})
	got, ok := attrs["config-directory"]
	if !ok {
		t.Fatal("update dropped config-directory=\"\" — there is no longer any " +
			"way to move a configure operation back to the root")
	}
	if got != "" {
		t.Fatalf("update sent config-directory = %#v, want \"\"", got)
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
		ConfigDirectory:          "ansible",
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
	if ws.ConfigDirectory != "ansible" {
		t.Errorf("ConfigDirectory = %q", ws.ConfigDirectory)
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
	// Turning the operation off is a BOOLEAN now (#1966), not a null on the
	// path. Driven through a fixture server rather than asserted on the attrs
	// map, so the request body is what is checked: `configure-enabled:false`
	// has to leave the process, and `config-directory` must not be touched —
	// a PATCH that also moved the path would reset where a re-enabled
	// operation would later run.
	f := newWorkspaceFixtureServer(t)
	f.updateHandler = func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"data":{"id":"ws-aaa","type":"workspaces","attributes":{
		  "name":"api","plan-apply-enabled":true,"configure-enabled":false,
		  "config-directory":"ansible"}}}`))
	}
	c := f.client()
	ws, err := c.UpdateWorkspace(t.Context(), "ws-aaa", UpdateWorkspaceRequest{
		ConfigureEnabled: boolPtr(false),
	})
	if err != nil {
		t.Fatalf("UpdateWorkspace: %v", err)
	}
	if ws.ConfigureEnabled {
		t.Error("ConfigureEnabled decoded as true from an explicit false")
	}
	if ws.ConfigDirectory != "ansible" {
		t.Errorf("ConfigDirectory = %q — turning the operation off must not move "+
			"the path it would run in", ws.ConfigDirectory)
	}
	if !strings.Contains(string(f.lastBody), `"configure-enabled":false`) {
		t.Errorf("request body did not carry the explicit false: %s", f.lastBody)
	}
	if strings.Contains(string(f.lastBody), "config-directory") {
		t.Errorf("request body touched config-directory, which the caller never "+
			"mentioned: %s", f.lastBody)
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

func boolPtr(b bool) *bool { return &b }
