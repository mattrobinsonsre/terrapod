package mcpserver

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// The module autodiscovery rule tools (#1584), through a real MCP client and
// server so every result passes the SDK's output-schema validation.

type moduleRuleCall struct {
	method, path, query string
	attrs               map[string]any
}

func callModuleRuleTool(t *testing.T, tool string, status int, body string, args map[string]any) (*mcp.CallToolResult, *moduleRuleCall) {
	t.Helper()
	got := &moduleRuleCall{}
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

const ruleListBody = `{"data":[{"id":"modrule-1","type":"module-autodiscovery-rules","attributes":{
  "name":"mg","vcs-connection-id":"vcs-2","repo-url":"https://github.com/org/terraform-azurerm-mg",
  "branch":"","pattern":"**/*.tf","ignore-patterns":[],"enabled":true,"name-template":"",
  "provider":"","vcs-tag-pattern":"v*","labels":{},"owner-email":"","first-scan-at":null,
  "last-scanned-sha":"","created-at":"2026-09-14T10:00:00Z","updated-at":"2026-09-14T10:00:00Z"}}],
  "meta":{"pagination":{"current-page":1,"page-size":100,"total-count":1,"total-pages":1}}}`

func TestModuleRuleListTool(t *testing.T) {
	res, got := callModuleRuleTool(t, "terrapod_module_autodiscovery_rule_list", http.StatusOK, ruleListBody, map[string]any{})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	if got.path != "/api/terrapod/v1/module-autodiscovery-rules" {
		t.Errorf("requested %s", got.path)
	}
	if out := resultText(t, res); !strings.Contains(out, "modrule-1") || !strings.Contains(out, `"count":1`) {
		t.Errorf("result: %s", out)
	}
}

func TestModuleRulePreviewTool(t *testing.T) {
	res, got := callModuleRuleTool(t, "terrapod_module_autodiscovery_rule_preview", http.StatusOK,
		`{"data":{"type":"module-autodiscovery-rule-previews","attributes":{"ref":"main","files-walked":3,"entries":[
		  {"subdirectory":"","name":"mg","provider":"azurerm","registered-as":null,"collision":false,"missing-provider":false},
		  {"subdirectory":"modules/create","name":"mg-create","provider":"azurerm","registered-as":{"name":"mg-create","provider":"azurerm"},"collision":false,"missing-provider":false}]}}}`,
		map[string]any{"rule_id": "modrule-1"})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	if got.method != http.MethodGet || got.path != "/api/terrapod/v1/module-autodiscovery-rules/modrule-1/preview" {
		t.Errorf("requested %s %s", got.method, got.path)
	}
	if out := resultText(t, res); !strings.Contains(out, "modules/create") {
		t.Errorf("result: %s", out)
	}
}

func TestModuleRulePreviewToolNeedsARule(t *testing.T) {
	res, got := callModuleRuleTool(t, "terrapod_module_autodiscovery_rule_preview", http.StatusOK, `{}`, map[string]any{})
	if !res.IsError || got.path != "" {
		t.Error("a missing rule_id should fail without calling the API")
	}
}

const scanBody = `{"data":{"type":"module-autodiscovery-rule-scans","attributes":{"ref":"main","files-walked":3,
  "modules-registered":1,"modules":[{"id":"m-1","name":"mg-create","provider":"azurerm","subdirectory":"modules/create"}],
  "skipped":[]}}}`

func TestModuleRuleScanToolRegistersAll(t *testing.T) {
	res, got := callModuleRuleTool(t, "terrapod_module_autodiscovery_rule_scan", http.StatusOK, scanBody,
		map[string]any{"rule_id": "modrule-1"})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	if got.method != http.MethodPost || got.path != "/api/terrapod/v1/module-autodiscovery-rules/modrule-1/scan" {
		t.Errorf("requested %s %s", got.method, got.path)
	}
	if _, ok := got.attrs["subdirectories"]; ok {
		t.Errorf("no subdirectories means all; sent %+v", got.attrs)
	}
	if out := resultText(t, res); !strings.Contains(out, `"modules-registered":1`) {
		t.Errorf("result: %s", out)
	}
}

func TestModuleRuleScanToolSendsTheChosenSubset(t *testing.T) {
	_, got := callModuleRuleTool(t, "terrapod_module_autodiscovery_rule_scan", http.StatusOK, scanBody,
		map[string]any{"rule_id": "modrule-1", "subdirectories": []string{"modules/create"}})
	sub, ok := got.attrs["subdirectories"].([]any)
	if !ok || len(sub) != 1 || sub[0] != "modules/create" {
		t.Errorf("sent %#v", got.attrs["subdirectories"])
	}
}

// ── Org-wide rules (#1620) ──────────────────────────────────────────

const orgPreviewBody = `{"data":{"type":"module-autodiscovery-rule-previews","attributes":{"ref":"","files-walked":0,
  "target-kind":"pattern","listing-complete":true,
  "entries":[{"repository":"org/terraform-aws-a","repo-url":"https://github.com/org/terraform-aws-a","subdirectory":"","name":"a","provider":"aws","registered-as":null,"collision":false,"missing-provider":false}],
  "repositories":[{"repository":"org/terraform-aws-a","repo-url":"https://github.com/org/terraform-aws-a","ref":"main","status":"active","origin":"new","error":""}]}},
  "meta":{"pagination":{"current-page":2,"page-size":1,"total-count":2,"total-pages":2}}}`

func TestModuleRulePreviewToolPagesAnOrgRule(t *testing.T) {
	res, got := callModuleRuleTool(t, "terrapod_module_autodiscovery_rule_preview", http.StatusOK, orgPreviewBody,
		map[string]any{"rule_id": "modrule-1", "page_number": 2, "page_size": 1})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	if got.query != "page%5Bnumber%5D=2&page%5Bsize%5D=1" {
		t.Errorf("query %q", got.query)
	}
	out := resultText(t, res)
	for _, want := range []string{`"repository":"org/terraform-aws-a"`, `"origin":"new"`, `"total-pages":2`, `"target-kind":"pattern"`} {
		if !strings.Contains(out, want) {
			t.Errorf("result lacks %s: %s", want, out)
		}
	}
}

func TestModuleRulePreviewToolReadsOneRepository(t *testing.T) {
	_, got := callModuleRuleTool(t, "terrapod_module_autodiscovery_rule_preview", http.StatusOK, orgPreviewBody,
		map[string]any{"rule_id": "modrule-1", "repository": "org/terraform-aws-a"})
	if got.query != "repository=org%2Fterraform-aws-a" {
		t.Errorf("query %q", got.query)
	}
}

const reposBody = `{"data":[{"id":"modrepo-1","type":"module-autodiscovery-rule-repositories","attributes":{
  "repository":"org/terraform-aws-a","repo-url":"https://github.com/org/terraform-aws-a","vcs-repo-id":"7","default-branch":"main",
  "origin":"baseline","status":"error","last-scanned-sha":"","seen-subdirectories":[],"candidates":[],"last-skips":[],
  "previous-paths":[],"repo-created-at":null,"first-seen-at":"2026-09-15T10:00:00Z","last-checked-at":null,
  "next-check-at":null,"failure-count":3,"last-error":"tree listing failed"}}],
  "meta":{"pagination":{"current-page":1,"page-size":50,"total-count":1,"total-pages":1}}}`

func TestModuleRuleRepositoriesTool(t *testing.T) {
	res, got := callModuleRuleTool(t, "terrapod_module_autodiscovery_rule_repositories", http.StatusOK, reposBody,
		map[string]any{"rule_id": "modrule-1", "status": "error"})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	if got.method != http.MethodGet || got.path != "/api/terrapod/v1/module-autodiscovery-rules/modrule-1/repositories" {
		t.Errorf("requested %s %s", got.method, got.path)
	}
	// A default page size, so an org with thousands of repositories cannot
	// flood the agent's context.
	if got.query != "filter%5Bstatus%5D=error&page%5Bsize%5D=50" {
		t.Errorf("query %q", got.query)
	}
	out := resultText(t, res)
	if !strings.Contains(out, `"last-error":"tree listing failed"`) || !strings.Contains(out, `"total-count":1`) {
		t.Errorf("result: %s", out)
	}
}

func TestModuleRuleRepositoriesToolNeedsARule(t *testing.T) {
	res, got := callModuleRuleTool(t, "terrapod_module_autodiscovery_rule_repositories", http.StatusOK, reposBody, map[string]any{})
	if !res.IsError || got.path != "" {
		t.Error("a missing rule_id should fail without calling the API")
	}
}

func TestModuleRuleScanToolSendsSelections(t *testing.T) {
	_, got := callModuleRuleTool(t, "terrapod_module_autodiscovery_rule_scan", http.StatusOK, scanBody,
		map[string]any{"rule_id": "modrule-1", "selections": []map[string]any{
			{"repository": "org/terraform-aws-a"},
			{"repository": "org/terraform-aws-b", "subdirectories": []string{"modules/x"}},
		}})
	sel, ok := got.attrs["selections"].([]any)
	if !ok || len(sel) != 2 {
		t.Fatalf("sent %#v", got.attrs)
	}
	if first := sel[0].(map[string]any); first["repository"] != "org/terraform-aws-a" || first["subdirectories"] != nil {
		t.Errorf("first selection %#v", first)
	}
	if second := sel[1].(map[string]any); second["subdirectories"] == nil {
		t.Errorf("second selection %#v", second)
	}
}

func TestModuleRuleScanToolRefusesBothForms(t *testing.T) {
	res, got := callModuleRuleTool(t, "terrapod_module_autodiscovery_rule_scan", http.StatusOK, scanBody,
		map[string]any{"rule_id": "modrule-1", "subdirectories": []string{""}, "selections": []map[string]any{{"repository": "org/a"}}})
	if !res.IsError || got.path != "" {
		t.Error("subdirectories with selections should fail without calling the API")
	}
}

func TestModuleRuleScanToolPassesARefusalThrough(t *testing.T) {
	res, _ := callModuleRuleTool(t, "terrapod_module_autodiscovery_rule_scan", http.StatusForbidden,
		`{"errors":[{"status":"403","detail":"Admin role required"}],"detail":"Admin role required"}`,
		map[string]any{"rule_id": "modrule-1"})
	if !res.IsError {
		t.Fatal("expected an error for a 403")
	}
}
