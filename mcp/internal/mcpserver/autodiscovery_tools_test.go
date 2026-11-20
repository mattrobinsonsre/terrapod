package mcpserver

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// The WORKSPACE autodiscovery rule tools (#2621), through a real MCP client and
// server so every result passes the SDK's output-schema validation.

type adRuleCall struct {
	method, path, query string
	attrs               map[string]any
	// options is the request-option block beside `data`, which is where
	// `propagate` and `dry_run` live -- they change what the update does
	// rather than what the rule says, so they are not attributes.
	options map[string]any
}

func callADRuleTool(t *testing.T, tool string, status int, body string, args map[string]any) (*mcp.CallToolResult, *adRuleCall) {
	t.Helper()
	got := &adRuleCall{}
	sess := toolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		got.method, got.path, got.query = r.Method, r.URL.Path, r.URL.RawQuery
		raw, _ := io.ReadAll(r.Body)
		if len(raw) > 0 {
			var doc struct {
				Data struct {
					Attributes map[string]any `json:"attributes"`
				} `json:"data"`
			}
			_ = json.Unmarshal(raw, &doc)
			got.attrs = doc.Data.Attributes
			// A request option sits at the document TOP LEVEL beside `data`,
			// which is what `MarshalResourceWithOptions` does -- there is no
			// "options" envelope, and looking for one reads every option as
			// absent.
			var top map[string]json.RawMessage
			_ = json.Unmarshal(raw, &top)
			delete(top, "data")
			if len(top) > 0 {
				got.options = map[string]any{}
				for k, v := range top {
					var parsed any
					_ = json.Unmarshal(v, &parsed)
					got.options[k] = parsed
				}
			}
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	})
	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{Name: tool, Arguments: args})
	if err != nil {
		t.Fatalf("CallTool %s: %.500v", tool, err)
	}
	return res, got
}

const adRuleListBody = `{"data":[{"id":"ad-1","type":"autodiscovery-rules","attributes":{
  "name":"monorepo","vcs-connection-id":"vcs-2","repo-url":"https://github.com/org/estate",
  "pattern":"services/*","enabled":true,"engine":"terraform","auto-apply-mode":"never",
  "created-at":"2026-09-14T10:00:00Z","updated-at":"2026-09-14T10:00:00Z"}}],
  "meta":{"pagination":{"current-page":1,"page-size":100,"total-count":1,"total-pages":1}}}`

func TestADRuleListTool(t *testing.T) {
	res, got := callADRuleTool(t, "terrapod_autodiscovery_rule_list", http.StatusOK, adRuleListBody, map[string]any{})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	if got.path != "/api/v1/autodiscovery-rules" {
		t.Errorf("requested %s", got.path)
	}
	out := resultText(t, res)
	if !strings.Contains(out, "ad-1") || !strings.Contains(out, `"count":1`) {
		t.Errorf("result: %s", out)
	}
	// The attribute bag is what makes a new server attribute reach an agent
	// without an SDK change, so it must survive the tool boundary.
	if !strings.Contains(out, "auto-apply-mode") {
		t.Errorf("the rule's own settings must reach the agent: %s", out)
	}
}

func TestADRuleGetTool(t *testing.T) {
	res, got := callADRuleTool(t, "terrapod_autodiscovery_rule_get", http.StatusOK,
		`{"data":{"id":"ad-1","type":"autodiscovery-rules","attributes":{"name":"monorepo","pattern":"services/*"}}}`,
		map[string]any{"rule_id": "ad-1"})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	if got.method != http.MethodGet || got.path != "/api/v1/autodiscovery-rules/ad-1" {
		t.Errorf("requested %s %s", got.method, got.path)
	}
}

func TestADRulePreviewToolBySavedRule(t *testing.T) {
	res, got := callADRuleTool(t, "terrapod_autodiscovery_rule_preview", http.StatusOK,
		`{"data":{"type":"autodiscovery-rule-previews","attributes":{"ref":"main","files-walked":9,"entries":[
		  {"workspace_name":"svc-a","working_directory":"services/a","collision":false,"existing_autodiscovered":true},
		  {"workspace_name":"svc-b","working_directory":"services/b","collision":true,"existing_autodiscovered":false}]}}}`,
		map[string]any{"rule_id": "ad-1"})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	if got.method != http.MethodGet || got.path != "/api/v1/autodiscovery-rules/ad-1/preview" {
		t.Errorf("requested %s %s", got.method, got.path)
	}
	out := resultText(t, res)
	// Both flags matter to an agent deciding whether to scan: a collision is
	// a workspace the rule will NOT take over, and an already-created one is
	// not new work.
	if !strings.Contains(out, "collision") || !strings.Contains(out, "existing_autodiscovered") {
		t.Errorf("the per-entry flags must reach the agent: %s", out)
	}
}

func TestADRulePreviewToolByUnsavedAttributes(t *testing.T) {
	res, got := callADRuleTool(t, "terrapod_autodiscovery_rule_preview", http.StatusOK,
		`{"data":{"type":"autodiscovery-rule-previews","attributes":{"ref":"main","files-walked":2,"entries":[]}}}`,
		map[string]any{"attributes": map[string]any{"pattern": "services/*"}})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	// The unsaved form must POST the collection's /preview, never a saved
	// rule's -- the two differ only by whether a rule exists.
	if got.method != http.MethodPost || got.path != "/api/v1/autodiscovery-rules/preview" {
		t.Errorf("requested %s %s", got.method, got.path)
	}
	if got.attrs["pattern"] != "services/*" {
		t.Errorf("attributes not sent: %v", got.attrs)
	}
}

func TestADRulePreviewToolRefusesBothFormsAtOnce(t *testing.T) {
	// Not a style point: the two preview DIFFERENT rules, so silently
	// picking one would report on something the caller did not ask about.
	res, _ := callADRuleTool(t, "terrapod_autodiscovery_rule_preview", http.StatusOK, `{}`,
		map[string]any{"rule_id": "ad-1", "attributes": map[string]any{"pattern": "x"}})
	if !res.IsError {
		t.Fatal("want an error when both rule_id and attributes are given")
	}
	if out := resultText(t, res); !strings.Contains(out, "not both") {
		t.Errorf("the refusal must say why: %s", out)
	}
}

func TestADRulePreviewToolRefusesNeitherForm(t *testing.T) {
	res, _ := callADRuleTool(t, "terrapod_autodiscovery_rule_preview", http.StatusOK, `{}`, map[string]any{})
	if !res.IsError {
		t.Fatal("want an error when neither rule_id nor attributes is given")
	}
}

func TestADRuleUpdateToolDoesNotPropagateByDefault(t *testing.T) {
	res, got := callADRuleTool(t, "terrapod_autodiscovery_rule_update", http.StatusOK,
		`{"data":{"id":"ad-1","type":"autodiscovery-rules","attributes":{"name":"monorepo"}}}`,
		map[string]any{"rule_id": "ad-1", "attributes": map[string]any{"pattern": "svc/*"}})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	if got.method != http.MethodPatch || got.path != "/api/v1/autodiscovery-rules/ad-1" {
		t.Errorf("requested %s %s", got.method, got.path)
	}
	// The default must not reach the workspaces the rule already created.
	// An option sent as false would be read by the server as a request to
	// propagate nothing, which is the same thing -- but sending nothing is
	// what the plain update path does, and that is the path being asserted.
	if got.options != nil {
		t.Errorf("no propagation was asked for, so no options belong on the wire: %v", got.options)
	}
}

func TestADRuleUpdateToolPropagatesWhenAsked(t *testing.T) {
	res, got := callADRuleTool(t, "terrapod_autodiscovery_rule_update", http.StatusOK,
		`{"data":{"id":"ad-1","type":"autodiscovery-rules","attributes":{"name":"monorepo"}},
		  "meta":{"propagation":{"dry_run":false,"matched":3,"would_change":[],"customised":[],"unchanged":[],"skipped_lifecycle":0}}}`,
		map[string]any{"rule_id": "ad-1", "attributes": map[string]any{"engine": "pulumi"}, "propagate": true})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	if got.options["propagate"] != true {
		t.Errorf("propagate must reach the wire as a request option: %v", got.options)
	}
	if _, ok := got.options["dry_run"]; ok {
		t.Errorf("dry_run was not asked for and must not be sent: %v", got.options)
	}
	// The report is the reason to use the tool rather than a plain PATCH.
	if out := resultText(t, res); !strings.Contains(out, "matched") {
		t.Errorf("the propagation report must reach the agent: %s", out)
	}
}

func TestADRuleUpdateToolSendsTheDryRunOption(t *testing.T) {
	res, got := callADRuleTool(t, "terrapod_autodiscovery_rule_update", http.StatusOK,
		`{"data":{"id":"ad-1","type":"autodiscovery-rules","attributes":{"name":"monorepo"}},
		  "meta":{"propagation":{"dry_run":true,"matched":2,"would_change":[],
		  "customised":[{"id":"ws-9","name":"svc-a","fields":{"engine":{"current":"pulumi","previous":"terraform","new":"pulumi"}}}],
		  "unchanged":[],"skipped_lifecycle":0}}}`,
		map[string]any{"rule_id": "ad-1", "attributes": map[string]any{"engine": "pulumi"}, "propagate": true, "dry_run": true})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	if got.options["propagate"] != true || got.options["dry_run"] != true {
		t.Errorf("both options must reach the wire: %v", got.options)
	}
	// `customised` is the whole value of the dry run: it names what would be
	// overwritten, which is the thing a caller cannot otherwise find out.
	// `customised` is the whole value of the dry run, and it means SKIPPED:
	// the field is left as the operator set it. The per-field current /
	// previous / new is what lets a caller see why.
	out := resultText(t, res)
	if !strings.Contains(out, "customised") || !strings.Contains(out, "ws-9") {
		t.Errorf("the dry-run report must carry customised: %s", out)
	}
	if !strings.Contains(out, "previous") {
		t.Errorf("a skipped field must say what it was compared against: %s", out)
	}
}

func TestADRuleUpdateToolRefusesADryRunWithoutPropagate(t *testing.T) {
	// Without propagate there is nothing to dry-run, so the call would
	// PERSIST while reading as a preview -- the one combination that is
	// dangerous rather than merely useless.
	res, got := callADRuleTool(t, "terrapod_autodiscovery_rule_update", http.StatusOK, `{}`,
		map[string]any{"rule_id": "ad-1", "attributes": map[string]any{"engine": "pulumi"}, "dry_run": true})
	if !res.IsError {
		t.Fatal("want an error for dry_run without propagate")
	}
	if got.method != "" {
		t.Errorf("it must refuse before writing anything, got %s %s", got.method, got.path)
	}
}

func TestADRuleScanToolReportsWhatItCreated(t *testing.T) {
	res, got := callADRuleTool(t, "terrapod_autodiscovery_rule_scan", http.StatusOK,
		`{"data":{"type":"autodiscovery-rule-scans","attributes":{"ref":"main","files-walked":9,
		  "workspaces-created":2,"workspace-ids":["ws-1","ws-2"]}}}`,
		map[string]any{"rule_id": "ad-1"})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	if got.method != http.MethodPost || got.path != "/api/v1/autodiscovery-rules/ad-1/scan" {
		t.Errorf("requested %s %s", got.method, got.path)
	}
	out := resultText(t, res)
	if !strings.Contains(out, "ws-1") || !strings.Contains(out, "workspaces-created") {
		t.Errorf("result: %s", out)
	}
}

func TestADRuleToolAnnotations(t *testing.T) {
	// `TestEveryToolIsAnnotated` already refuses an unannotated tool. This is
	// the specific claim: the three that write are not read-only, and delete
	// is DESTRUCTIVE rather than merely mutating -- deleting a rule cannot be
	// undone, so a host should be able to single it out for confirmation.
	want := map[string]string{
		"terrapod_autodiscovery_rule_list":    "read-only",
		"terrapod_autodiscovery_rule_get":     "read-only",
		"terrapod_autodiscovery_rule_preview": "read-only",
		"terrapod_autodiscovery_rule_create":  "mutating",
		"terrapod_autodiscovery_rule_update":  "mutating",
		"terrapod_autodiscovery_rule_scan":    "mutating",
		"terrapod_autodiscovery_rule_delete":  "destructive",
	}
	srv, _, err := New(Config{Host: "example.test", Name: "terrapod-test", Token: "test-token"})
	if err != nil {
		t.Fatalf("build server: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	clientT, serverT := mcp.NewInMemoryTransports()
	go func() { _ = srv.Run(ctx, serverT) }()
	client := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "0"}, nil)
	sess, err := client.Connect(ctx, clientT, nil)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer func() { _ = sess.Close() }()
	res, err := sess.ListTools(ctx, nil)
	if err != nil {
		t.Fatalf("list tools: %v", err)
	}
	seen := 0
	for _, tool := range res.Tools {
		expect, ours := want[tool.Name]
		if !ours {
			continue
		}
		seen++
		if tool.Annotations == nil {
			t.Errorf("%s has no annotations", tool.Name)
			continue
		}
		a := tool.Annotations
		destructive := a.DestructiveHint != nil && *a.DestructiveHint
		switch expect {
		case "read-only":
			if !a.ReadOnlyHint {
				t.Errorf("%s must be read-only: it writes nothing", tool.Name)
			}
		case "mutating":
			if a.ReadOnlyHint {
				t.Errorf("%s writes, so it must not be read-only", tool.Name)
			}
			if destructive {
				t.Errorf("%s is not destructive; annotating it so would put a "+
					"confirmation on every ordinary change", tool.Name)
			}
		case "destructive":
			if !destructive {
				t.Errorf("%s must be destructive: deleting a rule cannot be undone", tool.Name)
			}
		}
	}
	if seen != len(want) {
		t.Fatalf("only %d of %d rule tools are registered", seen, len(want))
	}
}

func TestADRuleToolsRequireTheirIDs(t *testing.T) {
	// An id-less call must refuse locally rather than requesting a path with
	// an empty segment, which the server would answer as the collection.
	for _, tool := range []string{
		"terrapod_autodiscovery_rule_get",
		"terrapod_autodiscovery_rule_scan",
		"terrapod_autodiscovery_rule_delete",
	} {
		res, got := callADRuleTool(t, tool, http.StatusOK, `{}`, map[string]any{})
		if !res.IsError {
			t.Errorf("%s: want an error with no rule_id", tool)
		}
		if got.method != "" {
			t.Errorf("%s: must not reach the server, got %s %s", tool, got.method, got.path)
		}
	}
}
