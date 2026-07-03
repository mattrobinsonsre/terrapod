package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
)

// ExecutionHook is a reusable custom-shell step run inside the runner Job at a
// fixed point (pre_init/pre_plan/post_plan/pre_apply/post_apply). A hook reaches
// a workspace only via explicit association (AssignWorkspaceToExecutionHook) —
// there is no global flag. Managing hooks requires platform admin.
type ExecutionHook struct {
	ID             string `json:"id"`
	Name           string `json:"name"`
	Description    string `json:"description,omitempty"`
	HookPoint      string `json:"hook-point"`
	Script         string `json:"script,omitempty"`
	Enabled        bool   `json:"enabled"`
	Priority       int64  `json:"priority"`
	WorkspaceCount int64  `json:"workspace-count"`
	CreatedAt      string `json:"created-at,omitempty"`
	UpdatedAt      string `json:"updated-at,omitempty"`
}

// CreateExecutionHookRequest is the input shape for CreateExecutionHook.
type CreateExecutionHookRequest struct {
	Name        string
	Description string
	HookPoint   string
	Script      string
	Enabled     bool
	Priority    int64
}

// UpdateExecutionHookRequest is the partial-update shape (nil pointer = leave
// unchanged).
type UpdateExecutionHookRequest struct {
	Name        string
	Description *string
	HookPoint   *string
	Script      *string
	Enabled     *bool
	Priority    *int64
}

// CreateExecutionHook creates a new execution hook.
func (c *Client) CreateExecutionHook(ctx context.Context, req CreateExecutionHookRequest) (*ExecutionHook, error) {
	body, err := MarshalResource("execution-hooks", executionHookCreateAttrs(req), nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create execution hook: %w", err)
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/execution-hooks", body)
	if err != nil {
		return nil, err
	}
	return parseExecutionHook(data)
}

// GetExecutionHook reads a hook by id.
func (c *Client) GetExecutionHook(ctx context.Context, id string) (*ExecutionHook, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/execution-hooks/"+url.PathEscape(id))
	if err != nil {
		return nil, err
	}
	return parseExecutionHook(data)
}

// ListExecutionHooks returns every execution hook.
func (c *Client) ListExecutionHooks(ctx context.Context) ([]ExecutionHook, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/execution-hooks")
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, err
	}
	out := make([]ExecutionHook, 0, len(resources))
	for i := range resources {
		out = append(out, *executionHookFromResource(&resources[i]))
	}
	return out, nil
}

// UpdateExecutionHook patches a hook.
func (c *Client) UpdateExecutionHook(ctx context.Context, id string, req UpdateExecutionHookRequest) (*ExecutionHook, error) {
	body, err := MarshalResourceWithID(id, "execution-hooks", executionHookUpdateAttrs(req))
	if err != nil {
		return nil, fmt.Errorf("marshal update execution hook: %w", err)
	}
	data, err := c.Patch(ctx, "/api/terrapod/v1/execution-hooks/"+url.PathEscape(id), body)
	if err != nil {
		return nil, err
	}
	return parseExecutionHook(data)
}

// DeleteExecutionHook removes a hook and all its workspace associations.
func (c *Client) DeleteExecutionHook(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/terrapod/v1/execution-hooks/"+url.PathEscape(id))
}

// ── Workspace association ────────────────────────────────────────────

// AssignWorkspaceToExecutionHook associates a workspace with a hook so the hook
// runs on that workspace's agent runs.
func (c *Client) AssignWorkspaceToExecutionHook(ctx context.Context, hookID, workspaceID string) error {
	body, err := marshalRelData([]relRef{{ID: workspaceID, Type: "workspaces"}})
	if err != nil {
		return fmt.Errorf("marshal assign body: %w", err)
	}
	_, err = c.Post(ctx,
		fmt.Sprintf("/api/terrapod/v1/execution-hooks/%s/relationships/workspaces", url.PathEscape(hookID)),
		body)
	return err
}

// UnassignWorkspaceFromExecutionHook removes the association. Idempotent.
func (c *Client) UnassignWorkspaceFromExecutionHook(ctx context.Context, hookID, workspaceID string) error {
	body, err := marshalRelData([]relRef{{ID: workspaceID, Type: "workspaces"}})
	if err != nil {
		return fmt.Errorf("marshal unassign body: %w", err)
	}
	return c.DeleteWithBody(ctx,
		fmt.Sprintf("/api/terrapod/v1/execution-hooks/%s/relationships/workspaces", url.PathEscape(hookID)),
		body)
}

// IsWorkspaceAssignedToExecutionHook reports whether the workspace is in the
// hook's `workspaces` relationship. Helper for Terraform Read paths.
func (c *Client) IsWorkspaceAssignedToExecutionHook(ctx context.Context, hookID, workspaceID string) (bool, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/execution-hooks/"+url.PathEscape(hookID))
	if err != nil {
		return false, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return false, err
	}
	rel, ok := res.Relationships["workspaces"]
	if !ok || len(rel.Data) == 0 || string(rel.Data) == "null" {
		return false, nil
	}
	var items []RelationshipResource
	if err := json.Unmarshal(rel.Data, &items); err != nil {
		return false, nil
	}
	for _, item := range items {
		if item.ID == workspaceID {
			return true, nil
		}
	}
	return false, nil
}

// ── Internal helpers ─────────────────────────────────────────────────

func executionHookCreateAttrs(req CreateExecutionHookRequest) map[string]any {
	attrs := map[string]any{
		"name":       req.Name,
		"hook-point": req.HookPoint,
		"script":     req.Script,
		"enabled":    req.Enabled,
		"priority":   req.Priority,
	}
	if req.Description != "" {
		attrs["description"] = req.Description
	}
	return attrs
}

func executionHookUpdateAttrs(req UpdateExecutionHookRequest) map[string]any {
	attrs := map[string]any{}
	if req.Name != "" {
		attrs["name"] = req.Name
	}
	if req.Description != nil {
		attrs["description"] = *req.Description
	}
	if req.HookPoint != nil {
		attrs["hook-point"] = *req.HookPoint
	}
	if req.Script != nil {
		attrs["script"] = *req.Script
	}
	if req.Enabled != nil {
		attrs["enabled"] = *req.Enabled
	}
	if req.Priority != nil {
		attrs["priority"] = *req.Priority
	}
	return attrs
}

func parseExecutionHook(body []byte) (*ExecutionHook, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse execution hook response: %w", err)
	}
	return executionHookFromResource(res), nil
}

func executionHookFromResource(res *Resource) *ExecutionHook {
	return &ExecutionHook{
		ID:             res.ID,
		Name:           GetStringAttr(res, "name"),
		Description:    GetStringAttr(res, "description"),
		HookPoint:      GetStringAttr(res, "hook-point"),
		Script:         GetStringAttr(res, "script"),
		Enabled:        GetBoolAttr(res, "enabled"),
		Priority:       GetIntAttr(res, "priority"),
		WorkspaceCount: GetIntAttr(res, "workspace-count"),
		CreatedAt:      GetStringAttr(res, "created-at"),
		UpdatedAt:      GetStringAttr(res, "updated-at"),
	}
}
