package mcpserver

import (
	"encoding/json"
	"io"
	"net/http"
	"testing"
)

// A workspace's operation declarations (#1966) and the ansible configure
// operation's roots (#1929) on the workspace tools.
//
// TestTheInventoryRootsAreReachableOnTheWorkspaceTools in inventory_tools_test.go
// asserts the tools ACCEPT these fields. That is a presence check and it is not
// enough: the input schema is built from the struct, so a field can be declared,
// advertised to an agent, accepted without complaint and then dropped on the
// floor because nobody passed it to the SDK call below it. Removing
// `ConfigDirectory: in.ConfigDirectory` from either handler leaves that schema
// gate green. These assert the WIRE, which is the only thing that can catch it
// -- the same defect `debug-mode` and `ai-policy-mode` shipped with in v1.8.0.
//
// # config_directory stopped being the declaration (#1966)
//
// It used to have three states, with null meaning "this workspace has no
// configure operation", and this file used to drive a `clear_config_directory`
// flag to reach that null. It has two states now -- "" is the configuration
// version's root and anything else is a subdirectory -- and the declaration
// moved to the `configure_enabled` boolean beside it. So the properties that
// bite here changed with it, and the ones below are chosen for what each would
// corrupt rather than merely lose:
//
//   - an explicit FALSE on either boolean must travel. Both are *bool for that
//     reason, and a handler that dropped a zero value would make
//     `plan_apply_enabled: false` -- the whole of a configure-only workspace --
//     a silent no-op.
//   - a call mentioning NEITHER boolean must send neither. `plan_apply_enabled`
//     defaults to TRUE, so a handler asserting a Go zero value would disable
//     plan/apply on every workspace an agent created or renamed.
//   - turning the configure operation off must NOT move `config_directory`.
//     A `config-directory` key appearing on that request would reset where a
//     re-enabled operation later runs.

// echoWorkspace answers a write with a minimal workspace so the tool's own
// decode succeeds and the test fails on the request rather than the response.
func echoWorkspace(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "application/vnd.api+json")
	_, _ = io.WriteString(w,
		`{"data":{"type":"workspaces","id":"ws-1","attributes":{"name":"configure-only"}}}`)
}

func TestWorkspaceCreateSendsEveryAnsibleRoot(t *testing.T) {
	var attrs map[string]any
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		echoWorkspace(w)
	})

	callTool(t, sess, "terrapod_workspace_create", map[string]any{
		"name":                       "configure-only",
		"config_directory":           "ansible",
		"inventory_paths":            []any{"inventory/production", "inventory/extra.yml"},
		"default_playbook":           "site.yml",
		"default_limit":              "web",
		"inventory_include_platform": false,
		"inventory_ignore_paths":     []any{"archive/"},
	})

	for key, want := range map[string]any{
		"config-directory":           "ansible",
		"default-playbook":           "site.yml",
		"default-limit":              "web",
		"inventory-include-platform": false,
	} {
		got, sent := attrs[key]
		if !sent {
			t.Errorf("create dropped %q entirely — the tool advertises the field and "+
				"discards it, so an agent setting it gets a success and no change", key)
			continue
		}
		if got != want {
			t.Errorf("create sent %q = %#v, want %#v", key, got, want)
		}
	}

	// Order is precedence for ansible, so it must survive the whole path.
	paths, ok := attrs["inventory-paths"].([]any)
	if !ok {
		t.Fatalf("create sent inventory-paths = %#v, want a list", attrs["inventory-paths"])
	}
	if len(paths) != 2 || paths[0] != "inventory/production" || paths[1] != "inventory/extra.yml" {
		t.Errorf("create sent inventory-paths = %v — the order IS the precedence", paths)
	}
	if ig, ok := attrs["inventory-ignore-paths"].([]any); !ok || len(ig) != 1 {
		t.Errorf("create sent inventory-ignore-paths = %#v", attrs["inventory-ignore-paths"])
	}
}

// A configure-only workspace: plan/apply off, configure on. The one create
// whose declarations are both the opposite of the server's defaults, so it is
// the one a handler that dropped Go zero values would get wrong in both
// directions at once.
func TestWorkspaceCreateSendsBothOperationDeclarations(t *testing.T) {
	var attrs map[string]any
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		echoWorkspace(w)
	})

	callTool(t, sess, "terrapod_workspace_create", map[string]any{
		"name":               "configure-only",
		"plan_apply_enabled": false,
		"configure_enabled":  true,
	})

	plan, sent := attrs["plan-apply-enabled"]
	if !sent {
		t.Fatal("create dropped an explicit plan_apply_enabled=false, so a configure-only " +
			"workspace cannot be created: the default is TRUE, and an agent that asked " +
			"for plan/apply off gets a success and a workspace that still has it")
	}
	if plan != false {
		t.Errorf("create sent plan-apply-enabled = %#v, want false", plan)
	}
	cfg, sent := attrs["configure-enabled"]
	if !sent {
		t.Fatal("create dropped configure_enabled, so the workspace has no configure " +
			"operation however its roots are set -- and config_directory does not " +
			"turn one on (#1966)")
	}
	if cfg != true {
		t.Errorf("create sent configure-enabled = %#v, want true", cfg)
	}
	// An empty config_directory is the ordinary way to root the operation at
	// the configuration version's root, and on CREATE it is also the server's
	// default -- so it need not travel. What must not happen is the reverse:
	// the operation being declared and nothing reaching the wire for it.
	if got, sent := attrs["config-directory"]; sent && got != "" {
		t.Errorf("create sent config-directory = %#v for a call that named none", got)
	}
}

// The other direction, and the one a plain `bool` would break: an explicit
// `configure_enabled: false` is a real request on create -- it is the default,
// but an agent restating it must not be silently upgraded to anything else.
func TestWorkspaceCreateSendsAnExplicitlyFalseConfigureEnabled(t *testing.T) {
	var attrs map[string]any
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		echoWorkspace(w)
	})

	callTool(t, sess, "terrapod_workspace_create", map[string]any{
		"name":              "api",
		"configure_enabled": false,
	})

	got, sent := attrs["configure-enabled"]
	if !sent {
		t.Fatal("create dropped an explicit configure_enabled=false")
	}
	if got != false {
		t.Errorf("create sent configure-enabled = %#v, want false", got)
	}
}

func TestWorkspaceCreateOmitsAnUnsetConfigDirectory(t *testing.T) {
	var attrs map[string]any
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		echoWorkspace(w)
	})

	callTool(t, sess, "terrapod_workspace_create", map[string]any{"name": "api"})

	if _, sent := attrs["config-directory"]; sent {
		t.Errorf("an unset config_directory was still sent (%#v), which would move "+
			"where a configure operation runs on a call that named no root",
			attrs["config-directory"])
	}
	// The dangerous one. `plan-apply-enabled` defaults to TRUE server-side, so
	// a handler that sent a Go zero value for an unmentioned field would
	// disable plan/apply on every workspace an agent creates.
	if got, sent := attrs["plan-apply-enabled"]; sent {
		t.Errorf("an unset plan_apply_enabled was still sent (%#v): the server's default "+
			"is TRUE, so asserting a value here would disable plan/apply on an ordinary "+
			"workspace create", got)
	}
	if got, sent := attrs["configure-enabled"]; sent {
		t.Errorf("an unset configure_enabled was still sent (%#v)", got)
	}
	for _, key := range []string{
		"inventory-paths", "default-playbook", "default-limit",
		"inventory-include-platform", "inventory-ignore-paths",
	} {
		if _, sent := attrs[key]; sent {
			t.Errorf("an unset %s was still sent: %#v", key, attrs[key])
		}
	}
}

func TestWorkspaceUpdateSendsEveryAnsibleRoot(t *testing.T) {
	var attrs map[string]any
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		echoWorkspace(w)
	})

	callTool(t, sess, "terrapod_workspace_update", map[string]any{
		"workspace_id":     "ws-1",
		"config_directory": "ansible",
		"inventory_paths":  []any{"inventory/production"},
		// Empty strings, because clearing is the direction that a plain
		// `!= ""` guard would drop: "stop defaulting to a playbook" and
		// "widen the run back to the whole inventory" are both expressed by "".
		"default_playbook":           "",
		"default_limit":              "",
		"inventory_include_platform": false,
		"inventory_ignore_paths":     []any{},
	})

	for key, want := range map[string]any{
		"config-directory":           "ansible",
		"default-playbook":           "",
		"default-limit":              "",
		"inventory-include-platform": false,
	} {
		got, sent := attrs[key]
		if !sent {
			t.Errorf("update dropped %q entirely", key)
			continue
		}
		if got != want {
			t.Errorf("update sent %q = %#v, want %#v", key, got, want)
		}
	}
	// An explicit empty list is the one request that removes every entry.
	if ig, sent := attrs["inventory-ignore-paths"]; !sent {
		t.Error("update dropped an explicit empty inventory_ignore_paths, so the one " +
			"call that clears it would silently do nothing")
	} else if l, ok := ig.([]any); !ok || len(l) != 0 {
		t.Errorf("update sent inventory-ignore-paths = %#v, want an empty list", ig)
	}
}

// Turning the configure operation OFF, which is `configure_enabled: false` and
// nothing else since #1966. Asserted on the BYTES as well as the decoded map,
// because the property is partly about a key being absent and a decoded map
// cannot tell an absent key from a JSON null.
//
// The path must not move. A `config-directory` key on this request would reset
// where the operation runs, so an operator turning it off for a release and
// back on afterwards would find it rooted somewhere else -- data lost by a
// request that looked like it only flipped a switch.
func TestWorkspaceUpdateTurnsTheConfigureOperationOffWithoutMovingItsRoot(t *testing.T) {
	var raw []byte
	var attrs map[string]any
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		raw, _ = io.ReadAll(r.Body)
		var body struct {
			Data struct {
				Attributes map[string]any `json:"attributes"`
			} `json:"data"`
		}
		if err := json.Unmarshal(raw, &body); err != nil {
			t.Errorf("write body is not JSON: %v: %s", err, raw)
		}
		attrs = body.Data.Attributes
		echoWorkspace(w)
	})

	callTool(t, sess, "terrapod_workspace_update", map[string]any{
		"workspace_id":      "ws-1",
		"configure_enabled": false,
	})

	got, sent := attrs["configure-enabled"]
	if !sent {
		t.Fatal("configure_enabled=false sent nothing, so an agent cannot turn the " +
			"configure operation off at all -- and the clear_config_directory flag " +
			"that used to do it is gone (#1966)")
	}
	if got != false {
		t.Errorf("update sent configure-enabled = %#v, want false", got)
	}
	if _, sent := attrs["config-directory"]; sent {
		t.Errorf("turning the configure operation off also sent config-directory "+
			"(%#v), which moves where a re-enabled operation would run",
			attrs["config-directory"])
	}
	// An explicit null is now a 422 naming the field, not a clear, so it must
	// not appear on any request this tool builds.
	if containsSubstring(string(raw), `"config-directory":null`) {
		t.Errorf("the request body carries an explicit null config-directory, which the "+
			"server refuses with a 422 since #1966: %s", raw)
	}
}

// The update's one remaining three-way distinction, and the reason
// ConfigDirectory is still a pointer here while the create tool takes a plain
// string: on a workspace rooted at `ansible`, "leave it alone" and "move it to
// the configuration version's root" are different requests.
func TestWorkspaceUpdateSendsAnEmptyConfigDirectoryToMoveTheRootToTheTop(t *testing.T) {
	var attrs map[string]any
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		echoWorkspace(w)
	})

	callTool(t, sess, "terrapod_workspace_update", map[string]any{
		"workspace_id":     "ws-1",
		"config_directory": "",
	})

	got, sent := attrs["config-directory"]
	if !sent {
		t.Fatal("an explicit empty config_directory was dropped, so a configure " +
			"operation rooted in a subdirectory can never be moved back to the " +
			"configuration version's root")
	}
	if got != "" {
		t.Errorf("update sent config-directory = %#v, want \"\"", got)
	}
	// Moving the root says nothing about whether the operation exists.
	if _, sent := attrs["configure-enabled"]; sent {
		t.Errorf("moving config_directory also sent configure-enabled (%#v): the path "+
			"and the declaration are separate fields since #1966",
			attrs["configure-enabled"])
	}
}

// Turning plan/apply off -- the configure-only direction on an existing
// workspace. The server refuses it unless the merged row keeps a configure
// operation, which is its judgement to make; what must hold here is that the
// request reaches it at all.
func TestWorkspaceUpdateSendsAnExplicitlyFalsePlanApplyEnabled(t *testing.T) {
	var attrs map[string]any
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		echoWorkspace(w)
	})

	callTool(t, sess, "terrapod_workspace_update", map[string]any{
		"workspace_id":       "ws-1",
		"plan_apply_enabled": false,
		"configure_enabled":  true,
	})

	plan, sent := attrs["plan-apply-enabled"]
	if !sent {
		t.Fatal("update dropped an explicit plan_apply_enabled=false, so an existing " +
			"workspace can never become configure-only")
	}
	if plan != false {
		t.Errorf("update sent plan-apply-enabled = %#v, want false", plan)
	}
	if cfg := attrs["configure-enabled"]; cfg != true {
		t.Errorf("update sent configure-enabled = %#v, want true", cfg)
	}
}

func TestWorkspaceUpdateOmitsUnsetAnsibleRoots(t *testing.T) {
	var attrs map[string]any
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		echoWorkspace(w)
	})

	callTool(t, sess, "terrapod_workspace_update", map[string]any{
		"workspace_id": "ws-1",
		"name":         "renamed",
	})

	for _, key := range []string{
		"config-directory", "inventory-paths", "default-playbook",
		"default-limit", "inventory-include-platform", "inventory-ignore-paths",
		// The declarations are in this list for the same reason and a
		// sharper one: a rename that also sent plan-apply-enabled would,
		// on a false Go zero value, silently retire the workspace.
		"plan-apply-enabled", "configure-enabled",
	} {
		if _, sent := attrs[key]; sent {
			t.Errorf("update sent %q (%#v) when the agent mentioned nothing about the "+
				"configure operation — a PATCH would overwrite a stored value", key, attrs[key])
		}
	}
}

// ── The read side ────────────────────────────────────────────────────────────

// terrapod_workspace_list projects the SDK's workspace onto a summary struct
// rather than returning it whole, so a field the SDK decodes perfectly well can
// still never reach the agent. That projection is the only thing between the
// two declarations and an agent orienting itself, and nothing else would
// report their absence: the catalogue golden freezes INPUT schemas, so an
// output field is pinned by no gate at all.
//
// The false case is the one that matters and the one an `omitempty` would eat.
// A configure-only workspace is exactly the workspace whose plan/apply is off,
// so dropping the false would hide the declaration on every workspace it
// actually describes — and an absent key reads as "not applicable" rather than
// "off", which is the wrong answer in the direction that makes an agent queue a
// plan that cannot run.
func TestWorkspaceListReportsBothOperationDeclarations(t *testing.T) {
	sess := toolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = io.WriteString(w, `{"data":[`+
			`{"type":"workspaces","id":"ws-1","attributes":{"name":"configure-only",`+
			`"plan-apply-enabled":false,"configure-enabled":true}},`+
			`{"type":"workspaces","id":"ws-2","attributes":{"name":"infra",`+
			`"plan-apply-enabled":true,"configure-enabled":false}}]}`)
	})

	out := callTool(t, sess, "terrapod_workspace_list", map[string]any{})

	listed, _ := out["workspaces"].([]any)
	if len(listed) != 2 {
		t.Fatalf("got %d workspaces, want 2", len(listed))
	}
	// Both rows are asserted because the property is a CONTRAST: a single
	// fixture would pass on a projection that hardcoded either value, and the
	// pair is what proves the field is being read off the workspace.
	for i, want := range []struct{ plan, cfg bool }{{false, true}, {true, false}} {
		row, _ := listed[i].(map[string]any)
		plan, sent := row["plan_apply_enabled"]
		if !sent {
			t.Errorf("workspace %d reports no plan_apply_enabled, so an agent must GET "+
				"each workspace to find out which tools apply to it", i)
		} else if plan != want.plan {
			t.Errorf("workspace %d reports plan_apply_enabled = %#v, want %v", i, plan, want.plan)
		}
		cfg, sent := row["configure_enabled"]
		if !sent {
			t.Errorf("workspace %d reports no configure_enabled, so an agent cannot tell "+
				"whether the inventory and configure tools apply to it", i)
		} else if cfg != want.cfg {
			t.Errorf("workspace %d reports configure_enabled = %#v, want %v", i, cfg, want.cfg)
		}
	}
}

func containsSubstring(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
