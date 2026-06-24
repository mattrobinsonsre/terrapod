package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
)

// WorkspaceFilter is the structured, server-side workspace selector used by
// SearchWorkspaces and BulkUpdateWorkspaces. All present dimensions are
// AND-combined. An empty filter is rejected (422) — set All to match every
// workspace on purpose. These map to the management API's
// /workspaces/actions/{search,bulk-update} endpoints (admin only).
type WorkspaceFilter struct {
	WorkspaceIDs     []string          `json:"workspace_ids,omitempty"`
	Labels           map[string]string `json:"labels,omitempty"`
	NamePrefix       string            `json:"name_prefix,omitempty"`
	NameGlob         string            `json:"name_glob,omitempty"`
	ExecutionBackend string            `json:"execution_backend,omitempty"`
	ExecutionMode    string            `json:"execution_mode,omitempty"`
	TerraformVersion string            `json:"terraform_version,omitempty"`
	AgentPoolID      string            `json:"agent_pool_id,omitempty"`
	VCSConnectionID  string            `json:"vcs_connection_id,omitempty"`
	OwnerEmail       string            `json:"owner_email,omitempty"`
	DriftStatus      string            `json:"drift_status,omitempty"`
	Locked           *bool             `json:"locked,omitempty"`
	HasVCS           *bool             `json:"has_vcs,omitempty"`
	All              bool              `json:"all,omitempty"`
}

// WorkspaceSummary is the trimmed workspace shape returned by the bulk
// search/preview endpoints (not a full JSON:API workspace resource).
type WorkspaceSummary struct {
	ID               string            `json:"id"`
	Name             string            `json:"name"`
	ExecutionMode    string            `json:"execution-mode"`
	ExecutionBackend string            `json:"execution-backend"`
	TerraformVersion string            `json:"terraform-version"`
	AgentPoolID      *string           `json:"agent-pool-id"`
	Labels           map[string]string `json:"labels"`
}

// SearchWorkspacesResult is the response from SearchWorkspaces.
type SearchWorkspacesResult struct {
	Matched    int                `json:"matched"`
	Workspaces []WorkspaceSummary `json:"workspaces"`
}

// SearchWorkspaces resolves a structured filter to the matching workspaces
// with no side effects (the discovery half of the bulk workflow). Admin
// only. An empty/zero filter returns a 422 ValidationError.
func (c *Client) SearchWorkspaces(ctx context.Context, filter WorkspaceFilter) (*SearchWorkspacesResult, error) {
	body, err := json.Marshal(map[string]any{"filter": filter})
	if err != nil {
		return nil, fmt.Errorf("marshal search filter: %w", err)
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/workspaces/actions/search", body)
	if err != nil {
		return nil, err
	}
	var out SearchWorkspacesResult
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, fmt.Errorf("parse search response: %w", err)
	}
	return &out, nil
}

// WorkspaceChange is one workspace's diff in a bulk-update result.
type WorkspaceChange struct {
	ID   string         `json:"id"`
	Name string         `json:"name"`
	Diff map[string]any `json:"diff,omitempty"`
}

// BulkUpdateResult is the response from BulkUpdateWorkspaces. When DryRun
// was true, WouldChange is populated; when false, Changes + Applied are.
type BulkUpdateResult struct {
	DryRun      bool              `json:"dry_run"`
	Matched     int               `json:"matched"`
	Applied     int               `json:"applied"`
	Changes     []WorkspaceChange `json:"changes,omitempty"`
	WouldChange []WorkspaceChange `json:"would_change,omitempty"`
	Unchanged   []WorkspaceChange `json:"unchanged,omitempty"`
	Errors      []string          `json:"errors,omitempty"`
}

// BulkUpdateWorkspaces applies `update` to every workspace matching
// `filter` in a single all-or-nothing transaction. `update` is the
// homogeneous settings/run-task/notification change set (hyphenated JSON:API
// attribute keys, e.g. "execution-mode", "terraform-version", "labels",
// "run-tasks", "notification-configurations"); it is validated once up
// front (422 on an invalid update). When dryRun is true the identical code
// path runs and rolls back, so the preview is exactly what an apply would
// change. Admin only; never triggers runs.
func (c *Client) BulkUpdateWorkspaces(
	ctx context.Context, filter WorkspaceFilter, update map[string]any, dryRun bool,
) (*BulkUpdateResult, error) {
	body, err := json.Marshal(map[string]any{
		"filter":  filter,
		"update":  update,
		"dry_run": dryRun,
	})
	if err != nil {
		return nil, fmt.Errorf("marshal bulk-update: %w", err)
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/workspaces/actions/bulk-update", body)
	if err != nil {
		return nil, err
	}
	var out BulkUpdateResult
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, fmt.Errorf("parse bulk-update response: %w", err)
	}
	return &out, nil
}
