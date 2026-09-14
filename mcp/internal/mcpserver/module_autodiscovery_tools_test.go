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
	method, path string
	attrs        map[string]any
}

func callModuleRuleTool(t *testing.T, tool string, status int, body string, args map[string]any) (*mcp.CallToolResult, *moduleRuleCall) {
	t.Helper()
	got := &moduleRuleCall{}
	sess := toolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		got.method, got.path = r.Method, r.URL.Path
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

func TestModuleRuleScanToolPassesARefusalThrough(t *testing.T) {
	res, _ := callModuleRuleTool(t, "terrapod_module_autodiscovery_rule_scan", http.StatusForbidden,
		`{"errors":[{"status":"403","detail":"Admin role required"}],"detail":"Admin role required"}`,
		map[string]any{"rule_id": "modrule-1"})
	if !res.IsError {
		t.Fatal("expected an error for a 403")
	}
}
