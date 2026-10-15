package mcpserver

import (
	"encoding/json"
	"io"
	"net/http"
	"regexp"
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

// TestExecutionBackendSaysWhichEngineItIsAbout guards the field most likely to
// be read as engine-neutral, on both tools that take it.
//
// `execution_backend` picks `tofu` vs `terraform` WITHIN the Terraform engine.
// Described as "tofu or terraform" and nothing more, it reads to an agent as the
// engine selector — which is `engine`, a different field on the same tool — so
// the two most plausible wrong actions are setting it to pick Pulumi, and setting
// it on a Pulumi workspace where it means nothing.
//
// The catalogue golden freezes field names and types, not descriptions, so this
// reads the live input schema.
func TestExecutionBackendSaysWhichEngineItIsAbout(t *testing.T) {
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
			desc := doc.Properties["execution_backend"].Description
			if desc == "" {
				t.Fatalf("%s has no execution_backend field", tool)
			}
			for _, want := range []string{"Terraform engine", "pulumi"} {
				if !strings.Contains(desc, want) {
					t.Errorf("execution_backend never mentions %q, so an agent cannot tell "+
						"which engine it is about:\n%s", want, desc)
				}
			}
		})
	}
}

// liveInputSchemasByName returns every registered tool's raw input schema,
// keyed by name — the sibling of liveToolsByName, for the field descriptions
// the golden does not freeze either.
func liveInputSchemasByName(t *testing.T) map[string]json.RawMessage {
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
	out := make(map[string]json.RawMessage, len(res.Tools))
	for _, tool := range res.Tools {
		raw, err := json.Marshal(tool.InputSchema)
		if err != nil {
			t.Fatalf("marshal %s input schema: %v", tool.Name, err)
		}
		out[tool.Name] = raw
	}
	if len(out) == 0 {
		t.Fatal("no tools registered")
	}
	return out
}

// ── The workspace-operations description guard (#1966) ───────────────────────

// TestWorkspaceToolsDoNotDescribeConfigDirectoryAsTheDeclaration guards the
// claim an agent acts on when it is asked to give a workspace an ansible
// configure operation, or to take one away.
//
// This is the same gap as TestTheInventoryToolsDoNotTellAnAgentAReadIsStale in
// inventory_tools_test.go, on the tools next door: the catalogue golden freezes
// names, annotations and input schemas and NOT descriptions, so every sentence
// below can go false with the whole suite green. It is not hypothetical on this
// surface — four inventory descriptions told agents a live read was stale for a
// release, and the reason MCP was the surface that shipped it is that the docs
// had a gate quoting the API's own literals and the web tab had an e2e
// assertion, while MCP had neither.
//
// What changed here is the shape of `config_directory`. It used to carry THREE
// states, with an absent value meaning "this workspace has no configure
// operation at all", so one field answered both where the operation runs and
// whether there is one. It is an ordinary path now — "" is the configuration
// version's root — and the declaration moved to the `configure_enabled` boolean
// beside it. The stale prose is dangerous in two specific directions, and
// neither is something an agent can sanity-check from a response: told that
// setting `config_directory` turns a configure operation on, it will set it,
// get a success, and report a configure-capable workspace that cannot run a
// playbook; told to omit the field to turn the operation off, it will send a
// PATCH that changes nothing and believe it worked.
//
// Deliberately NOT a golden of description text. A golden catches an unreviewed
// EDIT, and nobody edits these — they go stale underneath. What is pinned is
// the property, in both directions: the retired phrasings must be absent AND
// the replacement must be stated, because asserting absence alone would pass on
// a description that said nothing at all, which is the same disservice more
// quietly.
func TestWorkspaceToolsDoNotDescribeConfigDirectoryAsTheDeclaration(t *testing.T) {
	tools := liveToolsByName(t)
	schemas := liveInputSchemasByName(t)
	const create, update = "terrapod_workspace_create", "terrapod_workspace_update"

	// Phrasings that were true while `config_directory` was the declaration
	// and are now the wrong answer. Compared lowercased so a reintroduction in
	// any casing is caught.
	retired := []string{
		// The three-state model itself.
		"three states",
		"no configure operation at all",
		"the workspace has no configure operation",
		// config_directory as the switch.
		"which is what turns one on at all",
		"a workspace without it has none",
		"that turns the configure operation on",
		// The flag that used to reach the retired null. Naming it in a
		// description is worse than merely stale: an agent will try to pass
		// it, and the input schema refuses unknown properties.
		"clear_config_directory",
	}

	t.Run("retired phrasings appear nowhere", func(t *testing.T) {
		for _, tool := range []string{create, update} {
			desc, ok := tools[tool]
			if !ok {
				t.Fatalf("%s is not registered", tool)
			}
			// The tool description AND every one of its field descriptions,
			// because the three-state prose lived in the fields.
			haystacks := map[string]string{"(tool description)": desc}
			for field, fdesc := range fieldDescriptions(t, schemas, tool) {
				haystacks[field] = fdesc
			}
			for where, text := range haystacks {
				lower := strings.ToLower(text)
				for _, phrase := range retired {
					if strings.Contains(lower, phrase) {
						t.Errorf("%s %s carries a retired claim (%q): config_directory is an "+
							"ordinary path since #1966 with \"\" meaning the configuration "+
							"version's root, and configure_enabled is what declares the "+
							"operation:\n%s", tool, where, phrase, text)
					}
				}
			}
		}
	})

	// config_directory must say what it is NOT and name what is. An agent that
	// has only ever seen the three-state shape will otherwise carry it
	// forward, and nothing in the field's own text would contradict it.
	t.Run("config_directory disclaims the declaration", func(t *testing.T) {
		for _, tool := range []string{create, update} {
			desc := fieldDescriptions(t, schemas, tool)["config_directory"]
			if desc == "" {
				t.Fatalf("%s has no config_directory field", tool)
			}
			if !strings.Contains(desc, "configure_enabled") {
				t.Errorf("%s config_directory never names configure_enabled, so an agent "+
					"cannot tell what declares the operation:\n%s", tool, desc)
			}
			if !strings.Contains(desc, "does NOT") && !strings.Contains(desc, "cannot turn") {
				t.Errorf("%s config_directory does not disclaim being the declaration; an "+
					"agent reading it as the switch would set it and report a "+
					"configure-capable workspace with no configure operation:\n%s", tool, desc)
			}
			// The empty value is the trap in the other direction: read as a
			// missing value rather than "at the root", an agent would fill it
			// in, or report the workspace as misconfigured.
			//
			// A bare Contains("root") would NOT do here, and the reason is
			// worth keeping: every one of these descriptions says
			// "working_directory, which is the terraform/pulumi root" a
			// sentence later, so the word is present however little the
			// field says about its own empty value. The relationship has to
			// be matched, which means one sentence carrying both.
			if !emptyMeansRoot.MatchString(desc) {
				t.Errorf("%s config_directory does not say, in one sentence, that an EMPTY "+
					"value means the root, so \"\" reads as a gap rather than the "+
					"configuration version's root:\n%s", tool, desc)
			}
		}
	})

	// plan_apply_enabled defaults to TRUE, which is the most consequential
	// fact about it: an agent that thinks the default is false will set it on
	// every create, and one that thinks passing false is a no-op will retire a
	// workspace.
	t.Run("plan_apply_enabled states its default", func(t *testing.T) {
		for _, tool := range []string{create, update} {
			desc := fieldDescriptions(t, schemas, tool)[planApplyField]
			if desc == "" {
				t.Fatalf("%s has no %s field", tool, planApplyField)
			}
			if !strings.Contains(strings.ToLower(desc), "plan") {
				t.Errorf("%s %s never says which operation it governs:\n%s",
					tool, planApplyField, desc)
			}
		}
		// The default belongs on create, where omitting the field is the
		// normal case and the value it lands on is the whole question. On
		// update the equivalent fact is that omitting it leaves the stored
		// value alone, which the subtest below pins.
		//
		// Matched as the CLAIM and not as the word: a bare Contains("true")
		// passed with the default sentence deleted, because the same
		// description says "pass configure_enabled=true in the same call"
		// two sentences later. Found by mutation, which is the only thing
		// that could have found it.
		desc := fieldDescriptions(t, schemas, create)[planApplyField]
		if !defaultsTrue.MatchString(desc) {
			t.Errorf("%s %s does not state that the default is TRUE; an agent that assumed "+
				"false would set it on every workspace it created:\n%s",
				create, planApplyField, desc)
		}
	})

	// configure_enabled must claim the declaration config_directory disclaims,
	// or the two descriptions together point at nothing.
	t.Run("configure_enabled claims the declaration", func(t *testing.T) {
		for _, tool := range []string{create, update} {
			desc := fieldDescriptions(t, schemas, tool)["configure_enabled"]
			if desc == "" {
				t.Fatalf("%s has no configure_enabled field", tool)
			}
			if !strings.Contains(desc, "config_directory") {
				t.Errorf("%s configure_enabled never mentions config_directory, so an agent "+
					"cannot tell which of the two is the switch:\n%s", tool, desc)
			}
		}
		// On create the operation is opt-in; on update this field is the only
		// way off, and an agent that does not know that goes looking for
		// another one.
		if desc := fieldDescriptions(t, schemas, create)["configure_enabled"]; !defaultsFalse.MatchString(desc) &&
			!strings.Contains(desc, "opt-in") {
			t.Errorf("%s configure_enabled does not say the operation is off by "+
				"default:\n%s", create, desc)
		}
		if desc := fieldDescriptions(t, schemas, update)["configure_enabled"]; !strings.Contains(desc, "off") {
			t.Errorf("%s configure_enabled does not say it is how the operation is turned "+
				"off, so an agent will go looking for a field that does:\n%s", update, desc)
		}
	})

	// A PATCH that omits a declaration must be described as leaving it alone.
	// Silence here reads as "unset means false" on a field whose default is
	// TRUE, so an agent renaming a workspace would retire it.
	t.Run("the update describes omission as leaving a declaration alone", func(t *testing.T) {
		for _, field := range []string{planApplyField, "configure_enabled"} {
			desc := fieldDescriptions(t, schemas, update)[field]
			if !omissionLeavesItAlone.MatchString(desc) {
				t.Errorf("%s %s does not say that omitting it leaves the stored value "+
					"alone:\n%s", update, field, desc)
			}
		}
	})

	// Both operations off is refused, and the refusal has to be visible BEFORE
	// the call. An agent meeting it as a bare 422 will retry it, or conclude
	// the workspace is broken; told in advance, it pairs the two booleans or
	// reaches for the lock instead.
	t.Run("the both-off refusal is stated before the call", func(t *testing.T) {
		for _, tool := range []string{create, update} {
			text := tools[tool]
			for field, fdesc := range fieldDescriptions(t, schemas, tool) {
				if field == planApplyField || field == "configure_enabled" {
					text += "\n" + fdesc
				}
			}
			if !strings.Contains(text, "422") && !strings.Contains(strings.ToLower(text), "refused") {
				t.Errorf("%s never warns that turning both operations off is refused, so an "+
					"agent meets it as a bare error:\n%s", tool, text)
			}
		}
	})
}

// planApplyField is named once so a rename of the field is one edit here
// rather than a scatter of string literals that can drift apart.
const planApplyField = "plan_apply_enabled"

// Each of these matches a CLAIM rather than a word, and each replaced a
// Contains() that a mutation walked straight through. These descriptions are
// long and say several things, so any single word in them is likely to appear
// somewhere for an unrelated reason: "true" in `configure_enabled=true`,
// "root" in "working_directory, which is the terraform/pulumi root". Requiring
// the two halves of the claim within one sentence (`[^.]`, so the match cannot
// span a full stop) is what makes deleting the claim fail the test.
//
// Case-insensitive on purpose: the assertion is about the fact being stated,
// not about shouting it, so a later rewrite from "DEFAULTS TO TRUE" to
// "defaults to true" is not a regression and must not read as one.
var (
	defaultsTrue          = regexp.MustCompile(`(?i)default(s|ing)? (to |is )?true`)
	defaultsFalse         = regexp.MustCompile(`(?i)default(s|ing)? (to |is )?false`)
	emptyMeansRoot        = regexp.MustCompile(`(?i)empt(y|ied)[^.]{0,120}root`)
	omissionLeavesItAlone = regexp.MustCompile(`(?i)omitt(ing|ed)[^.]{0,120}(leaves|alone|unchanged)`)
)

// fieldDescriptions returns one tool's property descriptions keyed by field
// name — the per-field sibling of liveToolsByName, for the text an agent reads
// about an individual argument. The catalogue golden freezes neither.
func fieldDescriptions(t *testing.T, schemas map[string]json.RawMessage, tool string) map[string]string {
	t.Helper()
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
	out := make(map[string]string, len(doc.Properties))
	for name, prop := range doc.Properties {
		out[name] = prop.Description
	}
	return out
}
