package terrapod

import (
	"context"
	"fmt"
	"net/url"
)

// RunTrigger is a cross-workspace dependency — when the source
// workspace finishes applying, a run is queued on the destination
// workspace. The HCP/TFE model exposes triggers as a property of the
// destination, which is why creation is workspace-scoped.
type RunTrigger struct {
	ID             string `json:"id"`
	WorkspaceID    string `json:"workspace-id"`         // destination
	WorkspaceName  string `json:"workspace-name"`
	SourceID       string `json:"sourceable-id"`        // source
	SourceableName string `json:"sourceable-name"`
	CreatedAt      string `json:"created-at,omitempty"`
}

// CreateRunTriggerRequest links source → destination. The destination
// is the workspace under which the trigger is registered; the source
// is the workspace whose applies will fan out new runs.
type CreateRunTriggerRequest struct {
	DestinationWorkspaceID string
	SourceWorkspaceID      string
}

// CreateRunTrigger registers a new trigger. Caller must hold write+
// on the destination workspace.
func (c *Client) CreateRunTrigger(ctx context.Context, req CreateRunTriggerRequest) (*RunTrigger, error) {
	rels := map[string]any{
		"sourceable": map[string]any{
			"data": map[string]any{
				"id":   req.SourceWorkspaceID,
				"type": "workspaces",
			},
		},
	}
	body, err := MarshalResource("run-triggers", map[string]any{}, rels)
	if err != nil {
		return nil, fmt.Errorf("marshal create run-trigger: %w", err)
	}
	data, err := c.Post(ctx,
		fmt.Sprintf("/api/terrapod/v1/workspaces/%s/run-triggers", url.PathEscape(req.DestinationWorkspaceID)),
		body)
	if err != nil {
		return nil, err
	}
	return parseRunTrigger(data)
}

// GetRunTrigger reads a trigger by id.
func (c *Client) GetRunTrigger(ctx context.Context, id string) (*RunTrigger, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/run-triggers/"+url.PathEscape(id))
	if err != nil {
		return nil, err
	}
	return parseRunTrigger(data)
}

// ListInboundRunTriggers lists triggers whose destination is the
// given workspace (i.e. "this workspace will run when X applies").
func (c *Client) ListInboundRunTriggers(ctx context.Context, workspaceID string) ([]RunTrigger, error) {
	data, err := c.Get(ctx,
		fmt.Sprintf("/api/terrapod/v1/workspaces/%s/run-triggers?filter[run-trigger][type]=inbound",
			url.PathEscape(workspaceID)))
	if err != nil {
		return nil, err
	}
	return parseRunTriggerList(data)
}

// ListOutboundRunTriggers lists triggers whose source is the given
// workspace (i.e. "applying this workspace will queue runs on X").
func (c *Client) ListOutboundRunTriggers(ctx context.Context, workspaceID string) ([]RunTrigger, error) {
	data, err := c.Get(ctx,
		fmt.Sprintf("/api/terrapod/v1/workspaces/%s/run-triggers?filter[run-trigger][type]=outbound",
			url.PathEscape(workspaceID)))
	if err != nil {
		return nil, err
	}
	return parseRunTriggerList(data)
}

// DeleteRunTrigger removes a trigger by id. Idempotent on the server.
func (c *Client) DeleteRunTrigger(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/terrapod/v1/run-triggers/"+url.PathEscape(id))
}

// ── Internal helpers ─────────────────────────────────────────────────

func parseRunTrigger(body []byte) (*RunTrigger, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse run-trigger response: %w", err)
	}
	return runTriggerFromResource(res), nil
}

func parseRunTriggerList(body []byte) ([]RunTrigger, error) {
	resources, err := ParseResourceList(body)
	if err != nil {
		return nil, fmt.Errorf("parse run-trigger list: %w", err)
	}
	out := make([]RunTrigger, 0, len(resources))
	for i := range resources {
		out = append(out, *runTriggerFromResource(&resources[i]))
	}
	return out, nil
}

func runTriggerFromResource(res *Resource) *RunTrigger {
	t := &RunTrigger{
		ID:             res.ID,
		WorkspaceName:  GetStringAttr(res, "workspace-name"),
		SourceableName: GetStringAttr(res, "sourceable-name"),
		CreatedAt:      GetStringAttr(res, "created-at"),
	}
	if v := GetRelationshipID(res, "workspace"); v != "" {
		t.WorkspaceID = v
	}
	if v := GetRelationshipID(res, "sourceable"); v != "" {
		t.SourceID = v
	}
	return t
}
