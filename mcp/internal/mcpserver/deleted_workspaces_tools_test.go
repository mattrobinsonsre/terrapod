package mcpserver

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// The undelete tools (#1253) driven end to end: a real MCP client speaking to
// a real server over an in-memory transport, backed by a fake Terrapod.
//
// The catalogue golden pins that these tools are REGISTERED, with the right
// annotations and input schema. It says nothing about whether calling one
// reaches the right endpoint or returns anything useful — which is what a
// wrapper this thin can still get wrong (a typo'd path, a dropped argument, a
// result that never surfaces the fields an agent needs).

// toolCaller wires a fake Terrapod to an MCP client session.
func toolCaller(t *testing.T, handler http.HandlerFunc) *mcp.ClientSession {
	t.Helper()
	api := httptest.NewServer(handler)
	t.Cleanup(api.Close)

	c, err := terrapod.NewClient(terrapod.Options{BaseURL: api.URL, Token: "t"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}

	srv := mcp.NewServer(&mcp.Implementation{Name: "test", Version: "0"}, nil)
	registerObserve(srv, c)
	registerAct(srv, c)

	ct, st := mcp.NewInMemoryTransports()
	ctx := context.Background()
	if _, err := srv.Connect(ctx, st, nil); err != nil {
		t.Fatalf("server connect: %v", err)
	}
	sess, err := mcp.NewClient(&mcp.Implementation{Name: "test-client", Version: "0"}, nil).
		Connect(ctx, ct, nil)
	if err != nil {
		t.Fatalf("client connect: %v", err)
	}
	t.Cleanup(func() { _ = sess.Close() })
	return sess
}

func resultText(t *testing.T, res *mcp.CallToolResult) string {
	t.Helper()
	var sb strings.Builder
	for _, c := range res.Content {
		if tc, ok := c.(*mcp.TextContent); ok {
			sb.WriteString(tc.Text)
		}
	}
	return sb.String()
}

func TestDeletedWorkspaceListToolReturnsTheMarkers(t *testing.T) {
	var gotPath string
	sess := toolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(`{"data":[{"id":"11111111-1111-4111-8111-111111111111",
		  "type":"deleted-workspaces","attributes":{
		    "workspace-id":"11111111-1111-4111-8111-111111111111",
		    "workspace-name":"prod-network","deleted-at":"2026-08-01T09:00:00Z",
		    "marker-reason":"deleted","state-versions-available":12,
		    "restorable-until":"2026-08-31T09:00:00Z",
		    "variable-names":[{"key":"region","category":"terraform","sensitive":false}]}}],
		  "meta":{"pagination":{"current-page":1,"page-size":100,"total-pages":1,"total-count":1}}}`))
	})

	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "terrapod_deleted_workspace_list", Arguments: map[string]any{},
	})
	if err != nil {
		t.Fatalf("CallTool: %v", err)
	}
	if res.IsError {
		t.Fatalf("tool reported an error: %s", resultText(t, res))
	}
	if !strings.Contains(gotPath, "/api/terrapod/v1/deleted-workspaces") {
		t.Errorf("tool hit the wrong endpoint: %q", gotPath)
	}

	var out struct {
		Count   int                         `json:"count"`
		Deleted []terrapod.DeletedWorkspace `json:"deleted_workspaces"`
	}
	if err := json.Unmarshal([]byte(resultText(t, res)), &out); err != nil {
		t.Fatalf("decode tool result: %v (raw: %s)", err, resultText(t, res))
	}
	if out.Count != 1 || len(out.Deleted) != 1 {
		t.Fatalf("want 1 deleted workspace, got count=%d len=%d", out.Count, len(out.Deleted))
	}
	// The fields an agent needs in order to advise: what it was, and how long
	// is left to act.
	if out.Deleted[0].WorkspaceName != "prod-network" {
		t.Errorf("name not surfaced: %+v", out.Deleted[0])
	}
	if out.Deleted[0].RestorableUntil == "" || out.Deleted[0].StateVersionsAvailable != 12 {
		t.Errorf("window/count not surfaced: %+v", out.Deleted[0])
	}
}

func TestDeletedWorkspaceRestoreToolPostsAndReportsSuppression(t *testing.T) {
	var gotMethod, gotPath string
	sess := toolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		gotMethod, gotPath = r.Method, r.URL.Path
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(`{"data":{"id":"ws-99999999-9999-4999-8999-999999999999",
		  "type":"workspaces","attributes":{
		    "name":"prod-network","restored-from":"11111111-1111-4111-8111-111111111111",
		    "state-versions-restored":12,"state-versions-skipped":[],
		    "suppressed":["auto_apply","vcs_connection"],
		    "dropped-references":[{"field":"vcs","connection_id":"c-1"}]}}}`))
	})

	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name:      "terrapod_deleted_workspace_restore",
		Arguments: map[string]any{"workspace_id": "11111111-1111-4111-8111-111111111111"},
	})
	if err != nil {
		t.Fatalf("CallTool: %v", err)
	}
	if res.IsError {
		t.Fatalf("tool reported an error: %s", resultText(t, res))
	}
	if gotMethod != http.MethodPost || !strings.HasSuffix(gotPath, "/restore") {
		t.Errorf("want POST .../restore, got %s %s", gotMethod, gotPath)
	}

	var out terrapod.RestoredWorkspace
	if err := json.Unmarshal([]byte(resultText(t, res)), &out); err != nil {
		t.Fatalf("decode tool result: %v (raw: %s)", err, resultText(t, res))
	}
	// The suppression report is the whole point of the response: an agent must
	// be able to tell the user what did NOT come back.
	if len(out.Suppressed) != 2 {
		t.Errorf("suppression report not surfaced to the agent: %+v", out)
	}
	if out.StateVersionsRestored != 12 {
		t.Errorf("restored count not surfaced: %+v", out)
	}
}

func TestDeletedWorkspaceRestoreToolRequiresAWorkspaceID(t *testing.T) {
	// Guards against a silent no-arg call reaching the API and restoring
	// something unintended.
	called := false
	sess := toolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
		called = true
		w.WriteHeader(http.StatusOK)
	})

	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "terrapod_deleted_workspace_restore", Arguments: map[string]any{},
	})
	if err != nil {
		t.Fatalf("CallTool: %v", err)
	}
	if !res.IsError {
		t.Fatal("want an error result when workspace_id is missing")
	}
	if called {
		t.Error("the tool called the API despite a missing workspace_id")
	}
}

func TestDeletedWorkspaceRestoreToolSurfacesAConflict(t *testing.T) {
	// A restore past the retention window is a 409. The agent must see that as
	// an error rather than reporting a successful recovery of nothing.
	sess := toolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"errors":[{"detail":"No state could be recovered","status":"409"}]}`))
	})

	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name:      "terrapod_deleted_workspace_restore",
		Arguments: map[string]any{"workspace_id": "11111111-1111-4111-8111-111111111111"},
	})
	if err != nil {
		t.Fatalf("CallTool: %v", err)
	}
	if !res.IsError {
		t.Fatalf("a 409 must surface as an error, got: %s", resultText(t, res))
	}
	if !strings.Contains(resultText(t, res), "recovered") {
		t.Errorf("the reason should reach the agent, got: %s", resultText(t, res))
	}
}
