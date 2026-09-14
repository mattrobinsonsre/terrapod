package mcpserver

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// terrapod_catalog_item_interface (#1585), driven through a real MCP client
// and server so every call passes the SDK's validation of the tool's output
// against its schema — the check a mistyped output fails (see #1601).

func callCatalogItemInterface(t *testing.T, status int, body string, args map[string]any) (*mcp.CallToolResult, string) {
	t.Helper()
	var gotPath string
	sess := toolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	})
	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "terrapod_catalog_item_interface", Arguments: args,
	})
	if err != nil {
		t.Fatalf("CallTool: %.500v", err)
	}
	return res, gotPath
}

func TestCatalogItemInterfaceToolReturnsTheInterface(t *testing.T) {
	res, path := callCatalogItemInterface(t, http.StatusOK,
		`{"data":{"id":"ci-1","type":"catalog-item-interfaces","attributes":{
		  "resolved-version":"1.2.0",
		  "inputs":[{"name":"cidr","type":"string","description":"VPC CIDR","default":null,"required":true,"sensitive":false}],
		  "outputs":[{"name":"vpc_id","description":"The VPC","sensitive":false}]}}}`,
		map[string]any{"catalog_item_id": "ci-1"})

	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	if path != "/api/terrapod/v1/catalog-items/ci-1/interface" {
		t.Errorf("requested %s", path)
	}
	var out struct {
		ResolvedVersion string           `json:"resolved-version"`
		Inputs          []map[string]any `json:"inputs"`
		Outputs         []map[string]any `json:"outputs"`
	}
	if err := json.Unmarshal([]byte(resultText(t, res)), &out); err != nil {
		t.Fatal(err)
	}
	if out.ResolvedVersion != "1.2.0" || len(out.Inputs) != 1 || len(out.Outputs) != 1 {
		t.Errorf("result: %+v", out)
	}
}

func TestCatalogItemInterfaceToolBeforeAnyVersion(t *testing.T) {
	// Nulls must validate too, not only a populated interface.
	res, _ := callCatalogItemInterface(t, http.StatusOK,
		`{"data":{"id":"ci-1","type":"catalog-item-interfaces","attributes":{"resolved-version":null,"inputs":null,"outputs":null}}}`,
		map[string]any{"catalog_item_id": "ci-1"})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
}

func TestCatalogItemInterfaceToolPassesARefusalThrough(t *testing.T) {
	res, _ := callCatalogItemInterface(t, http.StatusForbidden,
		`{"errors":[{"status":"403","detail":"Requires catalog read on this item"}],"detail":"Requires catalog read on this item"}`,
		map[string]any{"catalog_item_id": "ci-1"})
	if !res.IsError || !strings.Contains(resultText(t, res), "permission denied") {
		t.Errorf("want a permission error, got %s", resultText(t, res))
	}
}

func TestCatalogItemInterfaceToolNeedsAnID(t *testing.T) {
	res, path := callCatalogItemInterface(t, http.StatusOK, `{}`, map[string]any{"catalog_item_id": ""})
	if !res.IsError {
		t.Error("expected an error for an empty id")
	}
	if path != "" {
		t.Error("an empty id should not reach the API")
	}
}
