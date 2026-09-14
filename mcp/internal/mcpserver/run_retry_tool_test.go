package mcpserver

import (
	"context"
	"net/http"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// terrapod_run_retry (#1599) driven end to end, through a real MCP client and
// server over the in-memory transport, against a fake Terrapod. The catalogue
// golden pins that the tool is registered and marked destructive; this pins
// that calling it reaches the retry endpoint and hands back the NEW run.

func TestRunRetryToolQueuesAndReturnsTheNewRun(t *testing.T) {
	var gotMethod, gotPath string
	sess := toolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		gotMethod, gotPath = r.Method, r.URL.Path
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"data":{"id":"run-bbbb","type":"runs",` +
			`"attributes":{"status":"pending","plan-only":true,"message":"Retry of run-aaaa"}}}`))
	})

	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "terrapod_run_retry", Arguments: map[string]any{"run_id": "run-aaaa"},
	})
	if err != nil {
		t.Fatalf("CallTool: %v", err)
	}
	if res.IsError {
		t.Fatalf("tool reported an error: %s", resultText(t, res))
	}
	if gotMethod != http.MethodPost || gotPath != "/api/terrapod/v1/runs/run-aaaa/actions/retry" {
		t.Errorf("tool hit %s %s, want POST /api/terrapod/v1/runs/run-aaaa/actions/retry", gotMethod, gotPath)
	}
	// The caller follows the new run, so its id has to come back.
	if out := resultText(t, res); !strings.Contains(out, "run-bbbb") {
		t.Errorf("result does not carry the new run's id: %s", out)
	}
}

func TestRunRetryToolPassesTheServersRefusalThrough(t *testing.T) {
	sess := toolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"errors":[{"status":"409","title":"Conflict",` +
			`"detail":"Cannot retry run in non-terminal state 'planning'"}],` +
			`"detail":"Cannot retry run in non-terminal state 'planning'"}`))
	})

	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "terrapod_run_retry", Arguments: map[string]any{"run_id": "run-busy"},
	})
	if err != nil {
		t.Fatalf("CallTool: %v", err)
	}
	if !res.IsError {
		t.Fatal("expected an error result for an unfinished run")
	}
	if out := resultText(t, res); !strings.Contains(out, "non-terminal state 'planning'") {
		t.Errorf("the server's reason was lost: %s", out)
	}
}

func TestRunRetryToolRequiresARunID(t *testing.T) {
	called := false
	sess := toolCaller(t, func(w http.ResponseWriter, r *http.Request) { called = true })

	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "terrapod_run_retry", Arguments: map[string]any{"run_id": ""},
	})
	if err != nil {
		t.Fatalf("CallTool: %v", err)
	}
	if !res.IsError {
		t.Fatal("expected an error for an empty run_id")
	}
	if called {
		t.Error("an empty run_id should not reach the API")
	}
}
