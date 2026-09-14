package mcpserver

import (
	"context"
	"net/http"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// terrapod_registry_module_discover (#1584), through a real MCP client and
// server so the result passes the SDK's output validation.

func callModuleDiscover(t *testing.T, status int, body string, args map[string]any) (*mcp.CallToolResult, string) {
	t.Helper()
	var got string
	sess := toolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		got = r.Method + " " + r.URL.Path
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	})
	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "terrapod_registry_module_discover", Arguments: args,
	})
	if err != nil {
		t.Fatalf("CallTool: %.500v", err)
	}
	return res, got
}

func TestModuleDiscoverToolReturnsTheCandidates(t *testing.T) {
	res, got := callModuleDiscover(t, http.StatusOK,
		`{"data":{"id":"d","type":"registry-module-discoveries","attributes":{
		  "vcs-repo-url":"https://github.com/org/terraform-azurerm-mg","vcs-branch":"main",
		  "candidates":[
		    {"subdirectory":"","suggested-name":"mg","suggested-provider":"azurerm","registered-as":null},
		    {"subdirectory":"modules/create","suggested-name":"mg-create","suggested-provider":"azurerm",
		     "registered-as":{"name":"mg-create","provider":"azurerm"}}]}}}`,
		map[string]any{"vcs_connection_id": "vcs-1", "vcs_repo_url": "https://github.com/org/terraform-azurerm-mg"})

	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	if got != "POST /api/terrapod/v1/registry-modules/discover" {
		t.Errorf("requested %s", got)
	}
	if out := resultText(t, res); !strings.Contains(out, "modules/create") || !strings.Contains(out, "mg-create") {
		t.Errorf("result missing the candidates: %s", out)
	}
}

func TestModuleDiscoverToolPassesARefusalThrough(t *testing.T) {
	res, _ := callModuleDiscover(t, http.StatusForbidden,
		`{"errors":[{"status":"403","detail":"Admin role required"}],"detail":"Admin role required"}`,
		map[string]any{"vcs_connection_id": "vcs-1", "vcs_repo_url": "https://github.com/org/r"})
	if !res.IsError {
		t.Fatal("expected an error for a 403")
	}
}

func TestModuleDiscoverToolNeedsConnectionAndRepo(t *testing.T) {
	res, got := callModuleDiscover(t, http.StatusOK, `{}`, map[string]any{"vcs_connection_id": "vcs-1"})
	if !res.IsError {
		t.Error("expected an error without a repository")
	}
	if got != "" {
		t.Error("missing arguments should not reach the API")
	}
}
