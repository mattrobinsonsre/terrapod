package mcpserver

import (
	"encoding/json"
	"io"
	"net/http"
	"testing"
)

// The ansible configure operation's roots on the workspace tools (#1929).
//
// TestTheInventoryRootsAreReachableOnTheWorkspaceTools in inventory_tools_test.go
// asserts the tools ACCEPT these fields. That is a presence check and it is not
// enough: the input schema is built from the struct, so a field can be declared,
// advertised to an agent, accepted without complaint and then dropped on the
// floor because nobody passed it to the SDK call below it. Removing
// `ConfigDirectory: in.ConfigDirectory` from either handler leaves that schema
// gate green. These assert the WIRE, which is the only thing that can catch it
// -- the same defect `debug-mode` and `ai-policy-mode` shipped with in v1.8.0.

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

// The repository-root case, which an emptiness guard anywhere along the path
// would silently turn into "no configure operation at all".
func TestWorkspaceCreateSendsAnEmptyConfigDirectory(t *testing.T) {
	var attrs map[string]any
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		echoWorkspace(w)
	})

	callTool(t, sess, "terrapod_workspace_create", map[string]any{
		"name":             "configure-only",
		"config_directory": "",
	})

	got, sent := attrs["config-directory"]
	if !sent {
		t.Fatal("an explicit empty config_directory was dropped, so there is no way to " +
			"root a configure operation at the top of a repository")
	}
	if got != "" {
		t.Errorf("create sent config-directory = %#v, want \"\"", got)
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
		t.Errorf("an unset config_directory was still sent (%#v), which would give a "+
			"terraform-only workspace a configure operation", attrs["config-directory"])
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

// Turning the configure operation OFF. The clear has to reach the wire as an
// explicit JSON null: `config-directory: ""` would root the operation at the
// repository root instead, which is the opposite of switching it off.
func TestWorkspaceUpdateClearsTheConfigureOperationWithAnExplicitNull(t *testing.T) {
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
		"workspace_id":           "ws-1",
		"clear_config_directory": true,
	})

	got, sent := attrs["config-directory"]
	if !sent {
		t.Fatal("clear_config_directory sent nothing, so an agent cannot turn the " +
			"configure operation off at all")
	}
	if got != nil {
		t.Errorf("clear_config_directory sent config-directory = %#v, want null", got)
	}
	// The decoded map cannot tell a JSON null from an absent key reliably once
	// it is in Go, so assert the bytes too.
	if !containsSubstring(string(raw), `"config-directory":null`) {
		t.Errorf("the request body carries no explicit null: %s", raw)
	}
}

// Both set is a caller bug, but it must resolve deterministically, and the
// documented resolution is that the clear wins — the safer of the two, since
// the alternative silently turns an operation ON at the repository root.
func TestWorkspaceUpdateClearWinsOverAConfigDirectoryValue(t *testing.T) {
	var attrs map[string]any
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		echoWorkspace(w)
	})

	callTool(t, sess, "terrapod_workspace_update", map[string]any{
		"workspace_id":           "ws-1",
		"config_directory":       "ansible",
		"clear_config_directory": true,
	})

	if got := attrs["config-directory"]; got != nil {
		t.Errorf("the clear did not win: config-directory = %#v, want null", got)
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
	} {
		if _, sent := attrs[key]; sent {
			t.Errorf("update sent %q (%#v) when the agent mentioned nothing about the "+
				"configure operation — a PATCH would overwrite a stored value", key, attrs[key])
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
