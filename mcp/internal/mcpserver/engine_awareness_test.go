package mcpserver

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// Terrapod runs more than one engine, and until #1911 the MCP surface did not
// say so: an agent could not narrow a workspace list to one engine, could not
// see which engine produced a run it was reading, could not create a Pulumi
// workspace with its bind-plan setting, and was told on connect that it was
// talking to a Terraform platform. Each of those is an agent confidently doing
// the wrong thing rather than asking, which is the failure worth pinning.

func callTool(t *testing.T, sess *mcp.ClientSession, name string, args map[string]any) map[string]any {
	t.Helper()
	res, err := sess.CallTool(t.Context(), &mcp.CallToolParams{Name: name, Arguments: args})
	if err != nil {
		t.Fatalf("CallTool %s: %v", name, err)
	}
	if res.IsError {
		t.Fatalf("%s tool error: %s", name, resultText(t, res))
	}
	var out map[string]any
	if err := json.Unmarshal(mustJSON(t, res.StructuredContent), &out); err != nil {
		t.Fatalf("decode %s: %v", name, err)
	}
	return out
}

// The engine filter has to reach the SERVER. Dropping it on the floor and
// returning every engine is the exact failure the filter exists to prevent,
// and it looks identical to a working filter from the agent's side when the
// instance happens to hold one engine.
func TestWorkspaceListSendsTheEngineFilterToTheServer(t *testing.T) {
	var gotQuery string
	sess := toolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		gotQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = io.WriteString(w, `{"data":[{"type":"workspaces","id":"ws-1",`+
			`"attributes":{"name":"infra","engine":"pulumi","execution-mode":"agent"}}]}`)
	})

	out := callTool(t, sess, "terrapod_workspace_list", map[string]any{"engine": "pulumi"})

	if !strings.Contains(gotQuery, "filter%5Bengine%5D=pulumi") {
		t.Errorf("engine filter never reached the server; query was %q", gotQuery)
	}
	// And the engine has to come back out, or the agent cannot tell what it got.
	ws, _ := out["workspaces"].([]any)
	if len(ws) != 1 {
		t.Fatalf("got %d workspaces, want 1", len(ws))
	}
	first, _ := ws[0].(map[string]any)
	if first["engine"] != "pulumi" {
		t.Errorf("listed workspace reports engine %v, want pulumi", first["engine"])
	}
}

// Omitting the filter must stay "every engine" — an agent orienting itself
// should see the whole estate, not silently a Terraform slice of it.
func TestWorkspaceListWithoutAnEngineFilterAsksForEveryEngine(t *testing.T) {
	var gotQuery string
	sess := toolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		gotQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = io.WriteString(w, `{"data":[]}`)
	})

	callTool(t, sess, "terrapod_workspace_list", map[string]any{})

	if strings.Contains(gotQuery, "filter") {
		t.Errorf("an unfiltered list sent a filter: %q", gotQuery)
	}
}

// A run inherits its workspace's engine, and several tools answer for one
// engine only. Without this the agent has to fetch the workspace separately to
// learn which tool it may use next — or, worse, guesses Terraform.
func TestRunListReportsTheEngineThatProducedEachRun(t *testing.T) {
	sess := toolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = io.WriteString(w, `{"data":[`+
			`{"type":"runs","id":"run-1","attributes":{"status":"planned","engine":"pulumi"}},`+
			`{"type":"runs","id":"run-2","attributes":{"status":"applied","engine":"terraform"}}]}`)
	})

	out := callTool(t, sess, "terrapod_run_list", map[string]any{"workspace_id": "ws-1"})

	runs, _ := out["runs"].([]any)
	if len(runs) != 2 {
		t.Fatalf("got %d runs, want 2", len(runs))
	}
	want := []string{"pulumi", "terraform"}
	for i, r := range runs {
		m, _ := r.(map[string]any)
		if m["engine"] != want[i] {
			t.Errorf("run %d reports engine %v, want %s", i, m["engine"], want[i])
		}
	}
}

// pulumi_bind_plan was settable on update and not on create, so an agent had to
// create a Pulumi workspace wrong and then correct it — two writes, and a
// window in which the workspace would run an unbound update.
func TestWorkspaceCreateSendsPulumiBindPlan(t *testing.T) {
	var gotBody []byte
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		gotBody, _ = io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = io.WriteString(w, `{"data":{"type":"workspaces","id":"ws-1",`+
			`"attributes":{"name":"stacks","engine":"pulumi","pulumi-bind-plan":true}}}`)
	})

	out := callTool(t, sess, "terrapod_workspace_create", map[string]any{
		"name": "stacks", "engine": "pulumi", "pulumi_bind_plan": true,
	})

	if !strings.Contains(string(gotBody), `"pulumi-bind-plan":true`) {
		t.Errorf("pulumi-bind-plan never reached the server; body was %s", gotBody)
	}
	if out["pulumi-bind-plan"] != true {
		t.Errorf("created workspace reports pulumi-bind-plan %v, want true", out["pulumi-bind-plan"])
	}
}

// Unset must be OMITTED, not sent as false. The server distinguishes them, and
// a create that always asserts false would silently overrule a deployment
// default — and would have to be refused on every non-Pulumi engine, breaking
// ordinary Terraform creates.
func TestWorkspaceCreateOmitsPulumiBindPlanWhenUnset(t *testing.T) {
	var gotBody []byte
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		gotBody, _ = io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = io.WriteString(w, `{"data":{"type":"workspaces","id":"ws-1","attributes":{"name":"infra"}}}`)
	})

	callTool(t, sess, "terrapod_workspace_create", map[string]any{"name": "infra"})

	if strings.Contains(string(gotBody), "pulumi-bind-plan") {
		t.Errorf("an unset pulumi_bind_plan was still sent: %s", gotBody)
	}
}

// The orientation text is the first thing an agent reads, and it was telling
// every agent that Terrapod is a Terraform platform. An agent primed that way
// does not think to check `engine` — it reaches for the Terraform tool and
// takes whatever comes back.
func TestInstructionsPrimeTheAgentToCheckTheEngine(t *testing.T) {
	got := instructions(Config{Host: "terrapod.example"})

	for _, want := range []string{"terrapod.example", "OpenTofu", "Pulumi", "engine"} {
		if !strings.Contains(got, want) {
			t.Errorf("orientation does not mention %q:\n%s", want, got)
		}
	}
	// Ansible is planned and does not ship. Orientation that claims an engine
	// the instance cannot run sends the agent looking for tools that do not
	// exist, and reads as a capability the operator was sold.
	if strings.Contains(strings.ToLower(got), "ansible") {
		t.Error("orientation claims Ansible, which does not ship")
	}
	// Open-source engines lead in prose (AGENTS.md → Conventions).
	if i, j := strings.Index(got, "OpenTofu"), strings.Index(got, "Terraform"); i < 0 || (j >= 0 && j < i) {
		t.Error("Terraform is named before OpenTofu; the open-source engine leads")
	}
	// The safety model must survive the rewrite.
	for _, want := range []string{"RBAC", "irreversible"} {
		if !strings.Contains(got, want) {
			t.Errorf("orientation lost the safety model (%q):\n%s", want, got)
		}
	}
}

func TestInstructionsStillNameTheEnvironment(t *testing.T) {
	got := instructions(Config{Host: "terrapod.example", EnvHint: "prod"})
	if !strings.Contains(got, "**prod**") {
		t.Errorf("orientation dropped the environment hint:\n%s", got)
	}
}

// A tool that answers for one engine only must SAY so. Generalising its wording
// to sound engine-neutral would be a lie the agent acts on; staying silent
// leaves it to find out by getting a wrong answer.
func TestSingleEngineToolsDeclareTheirEngine(t *testing.T) {
	tools := liveToolsByName(t)
	cases := []struct {
		tool string
		want []string
	}{
		// Reads `tofu show -json`; a Pulumi run writes a preview digest and the
		// compact view refuses it (see plan_engine_test.go).
		{"terrapod_run_plan_json", []string{"Pulumi"}},
		// compact_state_for_critique parses Terraform state v4 unconditionally.
		{"terrapod_workspace_architecture_critique", []string{"Pulumi"}},
		// Both engines evaluate policy sets; only Terraform runs are scanned.
		{"terrapod_run_policy_checks", []string{"Pulumi"}},
	}
	for _, tc := range cases {
		t.Run(tc.tool, func(t *testing.T) {
			desc, ok := tools[tc.tool]
			if !ok {
				t.Fatalf("tool %s is not registered", tc.tool)
			}
			for _, want := range tc.want {
				if !strings.Contains(desc, want) {
					t.Errorf("description never mentions %q, so an agent cannot tell it is engine-specific:\n%s", want, desc)
				}
			}
		})
	}
}

// liveToolsByName returns every registered tool's description, keyed by name.
// The catalogue golden freezes names, annotations and input schemas — not
// descriptions — so this reads the live server rather than the golden.
func liveToolsByName(t *testing.T) map[string]string {
	t.Helper()
	srv, _, err := New(Config{Host: "example.test", Name: "terrapod-test", Token: "test-token"})
	if err != nil {
		t.Fatalf("build server: %v", err)
	}
	ctx := t.Context()
	ct, st := mcp.NewInMemoryTransports()
	go func() { _ = srv.Run(ctx, st) }()
	sess, err := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "0"}, nil).Connect(ctx, ct, nil)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(func() { _ = sess.Close() })
	res, err := sess.ListTools(ctx, nil)
	if err != nil {
		t.Fatalf("list tools: %v", err)
	}
	out := make(map[string]string, len(res.Tools))
	for _, tool := range res.Tools {
		out[tool.Name] = tool.Description
	}
	if len(out) == 0 {
		t.Fatal("no tools registered")
	}
	return out
}
