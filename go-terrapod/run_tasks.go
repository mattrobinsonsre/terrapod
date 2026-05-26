package terrapod

import (
	"context"
	"fmt"
	"net/url"
)

// RunTask is a workspace-scoped pre/post-plan or pre-apply webhook.
// HMACKey is write-only (HasHMACKey indicates whether one is set).
type RunTask struct {
	ID               string `json:"id"`
	WorkspaceID      string `json:"workspace-id,omitempty"`
	Name             string `json:"name"`
	URL              string `json:"url"`
	Enabled          bool   `json:"enabled"`
	Stage            string `json:"stage"`             // pre_plan | post_plan | pre_apply
	EnforcementLevel string `json:"enforcement-level"` // mandatory | advisory
	HasHMACKey       bool   `json:"has-hmac-key"`
	CreatedAt        string `json:"created-at,omitempty"`
	UpdatedAt        string `json:"updated-at,omitempty"`
}

// CreateRunTaskRequest is the input shape for CreateRunTask. HMACKey
// is write-only.
type CreateRunTaskRequest struct {
	Name             string
	URL              string
	Stage            string
	EnforcementLevel string
	Enabled          *bool  // nil ⇒ server default (true)
	HMACKey          string
}

// UpdateRunTaskRequest is the partial-update shape. HMACKey
// non-empty ⇒ rotate, empty ⇒ leave alone.
type UpdateRunTaskRequest struct {
	Name             string
	URL              string
	Stage            string
	EnforcementLevel string
	Enabled          *bool
	HMACKey          string
}

// CreateRunTask creates a workspace-scoped run task.
func (c *Client) CreateRunTask(ctx context.Context, workspaceID string, req CreateRunTaskRequest) (*RunTask, error) {
	body, err := MarshalResource("run-tasks", runTaskCreateAttrs(req), nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create run-task: %w", err)
	}
	data, err := c.Post(ctx,
		fmt.Sprintf("/api/terrapod/v1/workspaces/%s/run-tasks", url.PathEscape(workspaceID)),
		body)
	if err != nil {
		return nil, err
	}
	return parseRunTask(data)
}

// GetRunTask reads a run task by id.
func (c *Client) GetRunTask(ctx context.Context, id string) (*RunTask, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/run-tasks/"+url.PathEscape(id))
	if err != nil {
		return nil, err
	}
	return parseRunTask(data)
}

// ListRunTasks returns every run task on a workspace.
func (c *Client) ListRunTasks(ctx context.Context, workspaceID string) ([]RunTask, error) {
	data, err := c.Get(ctx,
		fmt.Sprintf("/api/terrapod/v1/workspaces/%s/run-tasks", url.PathEscape(workspaceID)))
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, err
	}
	out := make([]RunTask, 0, len(resources))
	for i := range resources {
		out = append(out, *runTaskFromResource(&resources[i]))
	}
	return out, nil
}

// UpdateRunTask patches a run task.
func (c *Client) UpdateRunTask(ctx context.Context, id string, req UpdateRunTaskRequest) (*RunTask, error) {
	body, err := MarshalResourceWithID(id, "run-tasks", runTaskUpdateAttrs(req))
	if err != nil {
		return nil, fmt.Errorf("marshal update run-task: %w", err)
	}
	data, err := c.Patch(ctx, "/api/terrapod/v1/run-tasks/"+url.PathEscape(id), body)
	if err != nil {
		return nil, err
	}
	return parseRunTask(data)
}

// DeleteRunTask removes a run task.
func (c *Client) DeleteRunTask(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/terrapod/v1/run-tasks/"+url.PathEscape(id))
}

// ── Internal helpers ─────────────────────────────────────────────────

func runTaskCreateAttrs(req CreateRunTaskRequest) map[string]any {
	attrs := map[string]any{
		"name":              req.Name,
		"url":               req.URL,
		"stage":             req.Stage,
		"enforcement-level": req.EnforcementLevel,
	}
	if req.Enabled != nil {
		attrs["enabled"] = *req.Enabled
	}
	if req.HMACKey != "" {
		attrs["hmac-key"] = req.HMACKey
	}
	return attrs
}

func runTaskUpdateAttrs(req UpdateRunTaskRequest) map[string]any {
	attrs := map[string]any{}
	if req.Name != "" {
		attrs["name"] = req.Name
	}
	if req.URL != "" {
		attrs["url"] = req.URL
	}
	if req.Stage != "" {
		attrs["stage"] = req.Stage
	}
	if req.EnforcementLevel != "" {
		attrs["enforcement-level"] = req.EnforcementLevel
	}
	if req.Enabled != nil {
		attrs["enabled"] = *req.Enabled
	}
	if req.HMACKey != "" {
		attrs["hmac-key"] = req.HMACKey
	}
	return attrs
}

func parseRunTask(body []byte) (*RunTask, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse run-task response: %w", err)
	}
	return runTaskFromResource(res), nil
}

func runTaskFromResource(res *Resource) *RunTask {
	return &RunTask{
		ID:               res.ID,
		Name:             GetStringAttr(res, "name"),
		URL:              GetStringAttr(res, "url"),
		Enabled:          GetBoolAttr(res, "enabled"),
		Stage:            GetStringAttr(res, "stage"),
		EnforcementLevel: GetStringAttr(res, "enforcement-level"),
		HasHMACKey:       GetBoolAttr(res, "has-hmac-key"),
		CreatedAt:        GetStringAttr(res, "created-at"),
		UpdatedAt:        GetStringAttr(res, "updated-at"),
	}
}
