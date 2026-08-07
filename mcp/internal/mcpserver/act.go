package mcpserver

import (
	"context"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// ptrTrue / ptrBool are for the *bool annotation + request fields.
func ptrBool(b bool) *bool { return &b }

// readOnly marks a tool as non-mutating (Observe tools).
var readOnly = &mcp.ToolAnnotations{ReadOnlyHint: true}

// destructive marks a tool that can change or destroy real infrastructure, so
// an MCP host surfaces a confirmation — mirroring the UI's confirm-on-mutation
// policy.
var destructive = &mcp.ToolAnnotations{DestructiveHint: ptrBool(true)}

// registerAct adds the gated "Act" tools — queue a run and drive its lifecycle.
// Everything goes through the normal server-side run lifecycle: no bypass of
// plan-only, policy, VCS-apply rules, or RBAC. The token's capabilities decide
// what actually succeeds.
func registerAct(s *mcp.Server, c *terrapod.Client) {
	// ── terrapod_run_create ──────────────────────────────────────────
	type runCreateIn struct {
		WorkspaceID string `json:"workspace_id" jsonschema:"the workspace id (ws-...) to queue the run against"`
		// PlanOnly defaults true — a speculative plan that never applies. Set
		// false to request an apply-capable run (still gated: VCS-connected
		// workspaces reject CLI applies; non-VCS honour auto_apply/confirmation).
		PlanOnly  *bool  `json:"plan_only,omitempty" jsonschema:"plan only, no apply (default true — the safe choice)"`
		IsDestroy bool   `json:"is_destroy,omitempty" jsonschema:"plan a destroy (tear-down) run"`
		Message   string `json:"message,omitempty" jsonschema:"an optional human message describing why"`
		AutoApply *bool  `json:"auto_apply,omitempty" jsonschema:"auto-apply on success. Omit to use the workspace setting. Passing true does NOT mean 'use the workspace setting' — on a workspace configured with a conditional mode (create / create_update) an explicit true maps the run to 'always', applying it past a guardrail the operator set, including a plan containing destroys. Only pass it when you intend that for this one run."`
		Refresh   *bool  `json:"refresh,omitempty" jsonschema:"refresh state before planning (default true)"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_run_create",
		Description: "Queue a run against a workspace. Defaults to PLAN-ONLY (a safe speculative plan) — inspect the result with terrapod_run_get / terrapod_run_plan_json before applying. " +
			"Set plan_only=false only when the user explicitly wants to apply; the server still enforces gating (VCS workspaces block CLI applies; policy + auto-apply rules apply). Returns the created run.",
		Annotations: destructive,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in runCreateIn) (*mcp.CallToolResult, *terrapod.Run, error) {
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), nil, nil
		}
		planOnly := true
		if in.PlanOnly != nil {
			planOnly = *in.PlanOnly
		}
		run, err := c.CreateRun(ctx, terrapod.CreateRunRequest{
			WorkspaceID: in.WorkspaceID,
			PlanOnly:    planOnly,
			IsDestroy:   in.IsDestroy,
			Message:     in.Message,
			AutoApply:   in.AutoApply,
			Refresh:     in.Refresh,
		})
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, run, nil
	})

	// ── terrapod_run_apply ───────────────────────────────────────────
	type runIDIn struct {
		RunID string `json:"run_id" jsonschema:"the run id (run-...) to act on"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_run_apply",
		Description: "Confirm a PLANNED run so it applies — this changes real infrastructure and is effectively irreversible. Only call after the user has reviewed the plan and explicitly approved the apply. " +
			"Fails (gated) if the workspace is VCS-connected, manually locked, or the user's RBAC lacks apply.",
		Annotations: destructive,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in runIDIn) (*mcp.CallToolResult, *terrapod.Run, error) {
		if in.RunID == "" {
			return errText("run_id is required"), nil, nil
		}
		run, err := c.ApplyRun(ctx, in.RunID)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, run, nil
	})

	// ── terrapod_run_discard ─────────────────────────────────────────
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_run_discard",
		Description: "Discard a planned run without applying it (it will not change infrastructure). Use when a plan should not proceed.",
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in runIDIn) (*mcp.CallToolResult, *terrapod.Run, error) {
		if in.RunID == "" {
			return errText("run_id is required"), nil, nil
		}
		run, err := c.DiscardRun(ctx, in.RunID)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, run, nil
	})

	// ── terrapod_run_cancel ──────────────────────────────────────────
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_run_cancel",
		Description: "Cancel a non-terminal run (pending/planning/applying). Stops in-flight work; does not roll back an apply already completed.",
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in runIDIn) (*mcp.CallToolResult, *terrapod.Run, error) {
		if in.RunID == "" {
			return errText("run_id is required"), nil, nil
		}
		run, err := c.CancelRun(ctx, in.RunID)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, run, nil
	})

	// ── terrapod_run_security_scan_override ──────────────────────────
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_run_security_scan_override",
		Description: "Override a run's blocking IaC security scan so it can proceed despite failed/errored findings. Requires workspace admin. A run held in planning by an enforced scan is re-driven immediately. Use deliberately — this bypasses a security gate; prefer fixing the finding or adding a skip rule.",
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in runIDIn) (*mcp.CallToolResult, *terrapod.SecurityScan, error) {
		if in.RunID == "" {
			return errText("run_id is required"), nil, nil
		}
		sc, err := c.OverrideRunSecurityScan(ctx, in.RunID)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, sc, nil
	})

	// ── terrapod_deleted_workspace_restore ───────────────────────────
	type restoreIn struct {
		WorkspaceID string `json:"workspace_id" jsonschema:"the id of the DELETED workspace to recover, from terrapod_deleted_workspace_list; either the bare uuid or the ws- prefixed form"`
		Name        string `json:"name,omitempty" jsonschema:"optional name for the recovered workspace; when omitted the original name is reused, suffixed if it has since been taken. Must start with a letter or number and contain only letters, numbers, hyphens and underscores"`
		Force       bool   `json:"force,omitempty" jsonschema:"restore again even though this deletion has already been restored. Only set this when the earlier restored workspace is known to be gone — otherwise it produces a second live workspace over the same infrastructure"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_deleted_workspace_restore",
		Description: "Recover a deleted workspace's state into a NEW workspace. Requires platform admin. " +
			"This is a salvage operation, not an undo, and should be presented to the user that way: it creates a " +
			"workspace with a NEW id (the original is not revived), it comes back INERT with auto-apply and drift " +
			"detection off and the VCS connection not re-attached, and variables and run history do not come back — " +
			"only the state. The response's `suppressed` and `dropped-references` say what was deliberately left off " +
			"for the user to re-enable deliberately. Lineage and serial ARE preserved exactly, so the recovered " +
			"workspace continues the original state rather than starting a new one. Fails with a conflict if the " +
			"retention window has passed and the state has been reaped — check `restorable-until` from " +
			"terrapod_deleted_workspace_list first. Also fails with a conflict if this deletion has ALREADY been " +
			"restored (see `restored-to`), because a second restore would put a second live workspace with the same " +
			"state lineage over the same infrastructure; only pass force if the earlier one is known to be gone. " +
			"Only the newest state versions are copied — anything beyond the server's cap is reported in " +
			"`state-versions-skipped` rather than dropped silently. Because a restore materialises state (and " +
			"therefore secrets) into a workspace the caller can read, confirm with the user before calling it.",
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in restoreIn) (*mcp.CallToolResult, *terrapod.RestoredWorkspace, error) {
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), nil, nil
		}
		w, err := c.RestoreDeletedWorkspace(ctx, in.WorkspaceID, terrapod.RestoreOptions{Name: in.Name, Force: in.Force})
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, w, nil
	})
}
