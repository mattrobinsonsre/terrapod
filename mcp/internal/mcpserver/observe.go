package mcpserver

import (
	"context"
	"encoding/json"
	"errors"
	"strings"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// registerObserve adds the read-rich "Observe" tools — the surface a
// state-owning platform has that a CRUD-first MCP does not: reason about and
// diagnose the estate (workspaces, runs, plan JSON, …) before acting.
func registerObserve(s *mcp.Server, c *terrapod.Client) {
	// ── terrapod_workspace_list ──────────────────────────────────────
	type workspaceListIn struct {
		PageSize int `json:"page_size,omitempty" jsonschema:"max workspaces to return (default 50)"`
	}
	type workspaceSummary struct {
		ID            string            `json:"id"`
		Name          string            `json:"name"`
		ExecutionMode string            `json:"execution_mode"`
		Locked        bool              `json:"locked"`
		DriftStatus   string            `json:"drift_status,omitempty"`
		Labels        map[string]string `json:"labels,omitempty"`
	}
	type workspaceListOut struct {
		Count      int                `json:"count"`
		Workspaces []workspaceSummary `json:"workspaces"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_workspace_list",
		Description: "List workspaces on this Terrapod instance with their status, execution mode, lock state, drift status, and labels. Use this to orient before acting.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in workspaceListIn) (*mcp.CallToolResult, workspaceListOut, error) {
		size := in.PageSize
		if size <= 0 {
			size = 50
		}
		list, err := c.ListWorkspaces(ctx, terrapod.WorkspaceListOptions{PageSize: size})
		if err != nil {
			return errResult(err), workspaceListOut{}, nil
		}
		out := workspaceListOut{Count: len(list.Items)}
		for i := range list.Items {
			w := &list.Items[i]
			out.Workspaces = append(out.Workspaces, workspaceSummary{
				ID: w.ID, Name: w.Name, ExecutionMode: w.ExecutionMode,
				Locked: w.Locked, DriftStatus: w.DriftStatus, Labels: w.Labels,
			})
		}
		return nil, out, nil
	})

	// ── terrapod_workspace_get ───────────────────────────────────────
	type workspaceGetIn struct {
		Workspace string `json:"workspace" jsonschema:"the workspace id (ws-...) or its name"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_workspace_get",
		Description: "Get one workspace's full configuration and status by id (ws-...) or name — execution mode/backend, VCS wiring, labels, drift, lock, resource sizing.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in workspaceGetIn) (*mcp.CallToolResult, *terrapod.Workspace, error) {
		if in.Workspace == "" {
			return errText("workspace id or name is required"), nil, nil
		}
		var (
			w   *terrapod.Workspace
			err error
		)
		if strings.HasPrefix(in.Workspace, "ws-") {
			w, err = c.GetWorkspace(ctx, in.Workspace)
		} else {
			w, err = c.GetWorkspaceByName(ctx, in.Workspace)
		}
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, w, nil
	})

	// ── terrapod_run_list ────────────────────────────────────────────
	type runListIn struct {
		WorkspaceID string `json:"workspace_id" jsonschema:"the workspace id (ws-...) whose runs to list"`
		PageSize    int    `json:"page_size,omitempty" jsonschema:"max runs to return (default 20, newest first)"`
	}
	type runSummary struct {
		ID         string `json:"id"`
		Status     string `json:"status"`
		PlanOnly   bool   `json:"plan_only"`
		IsDestroy  bool   `json:"is_destroy"`
		HasChanges *bool  `json:"has_changes,omitempty"`
		Source     string `json:"source,omitempty"`
		CreatedAt  string `json:"created_at,omitempty"`
	}
	type runListOut struct {
		Count int          `json:"count"`
		Runs  []runSummary `json:"runs"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_run_list",
		Description: "List recent runs for a workspace (newest first) with status, whether each is plan-only/destroy, and whether the plan had changes.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in runListIn) (*mcp.CallToolResult, runListOut, error) {
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), runListOut{}, nil
		}
		size := in.PageSize
		if size <= 0 {
			size = 20
		}
		runs, err := c.ListWorkspaceRuns(ctx, in.WorkspaceID, 1, size)
		if err != nil {
			return errResult(err), runListOut{}, nil
		}
		out := runListOut{Count: len(runs)}
		for i := range runs {
			r := &runs[i]
			out.Runs = append(out.Runs, runSummary{
				ID: r.ID, Status: r.Status, PlanOnly: r.PlanOnly, IsDestroy: r.IsDestroy,
				HasChanges: r.HasChanges, Source: r.Source, CreatedAt: r.CreatedAt,
			})
		}
		return nil, out, nil
	})

	// ── terrapod_run_get ─────────────────────────────────────────────
	type runGetIn struct {
		RunID string `json:"run_id" jsonschema:"the run id (run-... or a bare uuid)"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_run_get",
		Description: "Get one run's full status incl. Terrapod-native detail: has-changes, drift flag, error message, the resource profile (peak memory / runner exit), and which lifecycle actions (apply/discard/cancel) are currently permitted.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in runGetIn) (*mcp.CallToolResult, *terrapod.Run, error) {
		if in.RunID == "" {
			return errText("run_id is required"), nil, nil
		}
		r, err := c.GetRun(ctx, in.RunID)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, r, nil
	})

	// ── terrapod_run_plan_json ───────────────────────────────────────
	type planJSONIn struct {
		RunID string `json:"run_id" jsonschema:"the run id whose structured JSON plan output to fetch"`
	}
	type planJSONOut struct {
		RunID    string          `json:"run_id"`
		PlanJSON json.RawMessage `json:"plan_json"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_run_plan_json",
		Description: "Fetch the structured JSON plan output (`tofu show -json`) for a run — the resource_changes, so you can reason precisely about what a plan will create/update/destroy. Returns 'not available' if the run produced no JSON plan.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in planJSONIn) (*mcp.CallToolResult, planJSONOut, error) {
		if in.RunID == "" {
			return errText("run_id is required"), planJSONOut{}, nil
		}
		raw, err := c.GetRunPlanJSON(ctx, in.RunID)
		if err != nil {
			return errResult(err), planJSONOut{}, nil
		}
		return nil, planJSONOut{RunID: in.RunID, PlanJSON: raw}, nil
	})
}

// errResult turns a go-terrapod error into an agent-facing tool error,
// distinguishing an expired token (actionable: re-login) from an RBAC denial
// (the user's Terrapod role lacks the capability — re-login won't help).
func errResult(err error) *mcp.CallToolResult {
	var (
		authnErr *terrapod.AuthenticationError
		authzErr *terrapod.AuthorizationError
	)
	switch {
	case errors.As(err, &authnErr):
		return errText("authentication failed — the Terrapod token is missing or expired. " +
			"Ask the user to run `tofu login <host>` and retry. (" + err.Error() + ")")
	case errors.As(err, &authzErr):
		return errText("permission denied — the user's Terrapod role lacks the capability for this action " +
			"(re-login won't help; an admin must grant it). (" + err.Error() + ")")
	default:
		return errText(err.Error())
	}
}

// errText builds a tool result flagged as an error carrying a message the agent
// relays to the user.
func errText(msg string) *mcp.CallToolResult {
	return &mcp.CallToolResult{
		IsError: true,
		Content: []mcp.Content{&mcp.TextContent{Text: msg}},
	}
}
