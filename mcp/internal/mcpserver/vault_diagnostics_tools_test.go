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

// The Vault diagnostics tools (#1663) driven end to end: a real MCP client
// against a real server, backed by a fake Terrapod. The catalogue golden pins
// registration; these pin that each tool reaches the right endpoint with the
// right body and surfaces the fields an agent needs.

func TestVaultStatusToolReturnsTheSample(t *testing.T) {
	var gotPath string
	sess := toolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		_, _ = w.Write([]byte(`{"data":[{"id":"default","type":"vault-instance-statuses",
		  "attributes":{"name":"default","auth-method":"kubernetes","tls-trust":"default",
		  "reachable":true,"sealed":true,"login-ok":null,"checked-at":"2026-09-15T10:00:00Z",
		  "last-error":{"class":"VaultUnavailable","message":"sealed","at":"2026-09-15T09:59:00Z"}}}],
		  "meta":{"vault":{"enabled":true,"sampled-at":"2026-09-15T10:00:00Z"}}}`))
	})

	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "terrapod_vault_status", Arguments: map[string]any{},
	})
	if err != nil || res.IsError {
		t.Fatalf("call: %v %s", err, resultText(t, res))
	}
	if gotPath != "/api/terrapod/v1/admin/vault" {
		t.Fatalf("path = %s", gotPath)
	}
	out := resultText(t, res)
	for _, want := range []string{`"sealed":true`, `"VaultUnavailable"`, `"enabled":true`} {
		if !strings.Contains(out, want) {
			t.Fatalf("result missing %s: %s", want, out)
		}
	}
	// Unknown stays null, never false.
	if !strings.Contains(out, `"login-ok":null`) {
		t.Fatalf("login-ok should be null: %s", out)
	}
}

func TestVaultReferenceCheckToolPostsTheReference(t *testing.T) {
	var gotPath string
	var gotBody map[string]any
	sess := toolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &gotBody)
		_, _ = w.Write([]byte(`{"data":{"id":"vrc-1","type":"vault-reference-checks","attributes":{
		  "ok":true,"parses":true,"engine":"dynamic","keys":null,"notes":["dynamic-not-read"],
		  "checks":[{"name":"readable","status":"pass","detail":""}]}}}`))
	})

	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "terrapod_vault_reference_check",
		Arguments: map[string]any{
			"workspace_id": "ws-1",
			"reference":    map[string]any{"mount": "database", "path": "creds/ro", "field": "password", "engine": "dynamic"},
		},
	})
	if err != nil || res.IsError {
		t.Fatalf("call: %v %s", err, resultText(t, res))
	}
	if gotPath != "/api/terrapod/v1/workspaces/ws-1/vault-reference-checks" {
		t.Fatalf("path = %s", gotPath)
	}
	ref := gotBody["data"].(map[string]any)["attributes"].(map[string]any)["reference"].(map[string]any)
	if ref["engine"] != "dynamic" || ref["path"] != "creds/ro" {
		t.Fatalf("reference = %v", ref)
	}
	if !strings.Contains(resultText(t, res), "dynamic-not-read") {
		t.Fatalf("result should carry the note: %s", resultText(t, res))
	}
}

func TestVaultReferenceCheckToolForAVariableSetVariable(t *testing.T) {
	var gotPath string
	sess := toolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		_, _ = w.Write([]byte(`{"data":{"id":"vrc-2","type":"vault-reference-checks","attributes":{"ok":true}}}`))
	})
	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name:      "terrapod_vault_reference_check",
		Arguments: map[string]any{"variable_set_id": "varset-7", "variable_id": "var-9"},
	})
	if err != nil || res.IsError {
		t.Fatalf("call: %v %s", err, resultText(t, res))
	}
	if gotPath != "/api/terrapod/v1/varsets/varset-7/vault-reference-checks" {
		t.Fatalf("path = %s", gotPath)
	}
}

func TestVaultReferenceCheckToolRefusesAmbiguousInput(t *testing.T) {
	called := false
	sess := toolCaller(t, func(w http.ResponseWriter, _ *http.Request) { called = true })
	for _, args := range []map[string]any{
		{"reference": map[string]any{"mount": "m"}},
		{"workspace_id": "ws-1", "variable_set_id": "varset-1", "variable_id": "var-1"},
		{"workspace_id": "ws-1"},
		{"workspace_id": "ws-1", "variable_id": "var-1", "reference": map[string]any{"mount": "m"}},
	} {
		res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
			Name: "terrapod_vault_reference_check", Arguments: args,
		})
		if err != nil {
			t.Fatalf("call: %v", err)
		}
		if !res.IsError {
			t.Fatalf("want a tool error for %v", args)
		}
	}
	if called {
		t.Fatal("ambiguous input must not reach Terrapod")
	}
}
