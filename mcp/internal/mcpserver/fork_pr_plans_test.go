package mcpserver

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
)

// allow-fork-pr-plans decides whether a pull request opened from a FORK gets a
// speculative plan (GHSA-gp5w-76rw-c452). That plan runs the fork author's code
// with the workspace's full credential set, and the author can neither write to
// the repository nor merge — so it is the only path by which their code reaches
// those credentials. Pull requests from branches in the repository itself are
// unaffected and always plan.
//
// Three things can go wrong on a thin wrapper like this, and each one is silent:
// the setting never reaches the server; an unset field is sent as false and
// overrules what the operator chose; or the agent is handed a switch whose
// description does not say what flipping it costs.

func TestWorkspaceCreateSendsAllowForkPRPlans(t *testing.T) {
	var attrs map[string]any
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = io.WriteString(w, `{"data":{"type":"workspaces","id":"ws-1",`+
			`"attributes":{"name":"infra","allow-fork-pr-plans":true}}}`)
	})

	out := callTool(t, sess, "terrapod_workspace_create", map[string]any{
		"name": "infra", "allow_fork_pr_plans": true,
	})

	if attrs["allow-fork-pr-plans"] != true {
		t.Errorf("allow-fork-pr-plans never reached the server; attributes were %v", attrs)
	}
	if out["allow-fork-pr-plans"] != true {
		t.Errorf("created workspace reports allow-fork-pr-plans %v, want true", out["allow-fork-pr-plans"])
	}
}

// Unset must be OMITTED. Sent as false it would read as a deliberate "turn this
// off", which is the same byte the operator would send to revoke it — harmless
// here only because the default happens to agree, and wrong the moment it does
// not.
func TestWorkspaceCreateOmitsAllowForkPRPlansWhenUnset(t *testing.T) {
	var attrs map[string]any
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = io.WriteString(w, `{"data":{"type":"workspaces","id":"ws-1","attributes":{"name":"infra"}}}`)
	})

	callTool(t, sess, "terrapod_workspace_create", map[string]any{"name": "infra"})

	if _, sent := attrs["allow-fork-pr-plans"]; sent {
		t.Errorf("an unset allow_fork_pr_plans was still sent: %v", attrs)
	}
}

// Turning it back OFF is the half a bare bool cannot express, and it is the
// direction that matters: an agent asked to revoke fork plans must actually
// revoke them rather than silently leave them on.
func TestWorkspaceUpdateSendsAllowForkPRPlansFalse(t *testing.T) {
	var attrs map[string]any
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = io.WriteString(w, `{"data":{"type":"workspaces","id":"ws-1",`+
			`"attributes":{"name":"infra","allow-fork-pr-plans":false}}}`)
	})

	callTool(t, sess, "terrapod_workspace_update", map[string]any{
		"workspace_id": "ws-1", "allow_fork_pr_plans": false,
	})

	got, sent := attrs["allow-fork-pr-plans"]
	if !sent {
		t.Fatalf("an explicit false was dropped, so the setting could not be turned off: %v", attrs)
	}
	if got != false {
		t.Errorf("update sent allow-fork-pr-plans %v, want false", got)
	}
}

func TestWorkspaceUpdateOmitsAllowForkPRPlansWhenUnset(t *testing.T) {
	var attrs map[string]any
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = io.WriteString(w, `{"data":{"type":"workspaces","id":"ws-1","attributes":{"name":"renamed"}}}`)
	})

	callTool(t, sess, "terrapod_workspace_update", map[string]any{
		"workspace_id": "ws-1", "name": "renamed",
	})

	if _, sent := attrs["allow-fork-pr-plans"]; sent {
		t.Errorf("an update that never mentioned the setting still sent it: %v", attrs)
	}
}

// Reading it back is the other half: an agent auditing an estate has to be able
// to see which workspaces plan fork pull requests. terrapod_workspace_get
// returns the SDK's Workspace whole, so this pins that the field survives that
// round trip rather than being dropped by a curated projection.
func TestWorkspaceGetSurfacesAllowForkPRPlans(t *testing.T) {
	sess := toolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = io.WriteString(w, `{"data":{"type":"workspaces","id":"ws-1",`+
			`"attributes":{"name":"infra","allow-fork-pr-plans":true}}}`)
	})

	out := callTool(t, sess, "terrapod_workspace_get", map[string]any{"workspace": "ws-1"})

	if out["allow-fork-pr-plans"] != true {
		t.Errorf("workspace_get reports allow-fork-pr-plans %v, want true", out["allow-fork-pr-plans"])
	}
}

// The catalogue golden freezes field names and types, not descriptions — and
// the description is the whole safety mechanism here. An agent handed a bare
// "allow fork PR plans" boolean has no way to know it is granting untrusted
// code the workspace's credentials, so it will enable it whenever that unblocks
// the task in front of it.
func TestAllowForkPRPlansDescriptionNamesTheConsequence(t *testing.T) {
	schemas := liveInputSchemasByName(t)
	for _, tool := range []string{"terrapod_workspace_create", "terrapod_workspace_update"} {
		t.Run(tool, func(t *testing.T) {
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
				t.Fatalf("decode input schema: %v", err)
			}
			desc := doc.Properties["allow_fork_pr_plans"].Description
			if desc == "" {
				t.Fatalf("%s has no allow_fork_pr_plans field", tool)
			}
			// "Off by default" from this release: the phrase has to match THIS
			// line's default, or the description tells an agent the opposite of
			// what the server will do. It said "On by default" while 1.8 shipped
			// the permissive default, and the flip to a closed default had to move
			// this with it. The property the assertion holds is unchanged — the
			// description states the default, names the credentials at stake, and
			// says the author cannot merge.
			for _, want := range []string{"fork", "credential", "Off by default", "merge"} {
				if !strings.Contains(desc, want) {
					t.Errorf("description never mentions %q, so an agent cannot weigh "+
						"enabling it:\n%s", want, desc)
				}
			}
		})
	}
}

// The tool is a config write, not a destroy: its existing annotation must not
// drift just because a security-relevant field moved in behind it.
func TestWorkspaceWriteToolsKeepTheirMutatingAnnotation(t *testing.T) {
	byName := map[string]toolEntry{}
	for _, e := range liveCatalogue(t) {
		byName[e.Name] = e
	}
	for _, n := range []string{"terrapod_workspace_create", "terrapod_workspace_update"} {
		e, ok := byName[n]
		if !ok {
			t.Errorf("missing tool %q", n)
			continue
		}
		if e.ReadOnly {
			t.Errorf("tool %q is marked read-only but writes workspace config", n)
		}
		if e.Destructive {
			t.Errorf("tool %q is marked destructive; it is a config write", n)
		}
	}
}
