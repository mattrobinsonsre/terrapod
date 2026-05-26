package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
)

// AgentPool is the decoded form of a Terrapod agent pool — the named
// group of runner listeners that pick up runs for matching workspaces.
// Labels carry RBAC + workspace-binding semantics on the server side.
type AgentPool struct {
	ID          string            `json:"id"`
	Name        string            `json:"name"`
	Description string            `json:"description,omitempty"`
	Labels      map[string]string `json:"labels,omitempty"`
	OwnerEmail  string            `json:"owner-email,omitempty"`
	CreatedAt   string            `json:"created-at,omitempty"`
	UpdatedAt   string            `json:"updated-at,omitempty"`
}

// CreateAgentPoolRequest is the input shape for
// Client.CreateAgentPool. Labels and OwnerEmail are optional. The
// pool's labels gate listener placement and workspace assignment
// (see the workspace RBAC rules).
type CreateAgentPoolRequest struct {
	Name        string
	Description string
	Labels      map[string]string
	OwnerEmail  string
}

// UpdateAgentPoolRequest is the partial-update shape. Pointer-typed
// fields preserve "leave alone" semantics so a vanilla rename doesn't
// inadvertently clear labels or owner. The Labels field is a pointer
// so &{} explicitly clears all labels, nil leaves them intact.
type UpdateAgentPoolRequest struct {
	Name        string
	Description *string
	Labels      *map[string]string
	OwnerEmail  *string
}

// CreateAgentPool creates a new pool. Admin role is required.
func (c *Client) CreateAgentPool(ctx context.Context, req CreateAgentPoolRequest) (*AgentPool, error) {
	body, err := MarshalResource("agent-pools", agentPoolCreateAttrs(req), nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create agent-pool: %w", err)
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/agent-pools", body)
	if err != nil {
		return nil, err
	}
	return parseAgentPool(data)
}

// GetAgentPool reads a pool by id.
func (c *Client) GetAgentPool(ctx context.Context, id string) (*AgentPool, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/agent-pools/"+url.PathEscape(id))
	if err != nil {
		return nil, err
	}
	return parseAgentPool(data)
}

// ListAgentPools returns every pool visible to the caller. Result
// scope is determined by pool RBAC (admin sees all; regular users see
// pools they have read access to).
func (c *Client) ListAgentPools(ctx context.Context) ([]AgentPool, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/agent-pools")
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, err
	}
	out := make([]AgentPool, 0, len(resources))
	for i := range resources {
		out = append(out, *agentPoolFromResource(&resources[i]))
	}
	return out, nil
}

// UpdateAgentPool patches the pool. Caller must hold pool admin.
func (c *Client) UpdateAgentPool(ctx context.Context, id string, req UpdateAgentPoolRequest) (*AgentPool, error) {
	body, err := MarshalResourceWithID(id, "agent-pools", agentPoolUpdateAttrs(req))
	if err != nil {
		return nil, fmt.Errorf("marshal update agent-pool: %w", err)
	}
	data, err := c.Patch(ctx, "/api/terrapod/v1/agent-pools/"+url.PathEscape(id), body)
	if err != nil {
		return nil, err
	}
	return parseAgentPool(data)
}

// DeleteAgentPool removes the pool. Workspaces referencing it lose
// their pool binding; listeners in the pool are evicted.
func (c *Client) DeleteAgentPool(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/terrapod/v1/agent-pools/"+url.PathEscape(id))
}

// ── Internal helpers ─────────────────────────────────────────────────

func agentPoolCreateAttrs(req CreateAgentPoolRequest) map[string]any {
	attrs := map[string]any{
		"name": req.Name,
	}
	if req.Description != "" {
		attrs["description"] = req.Description
	}
	if req.Labels != nil {
		attrs["labels"] = req.Labels
	}
	if req.OwnerEmail != "" {
		attrs["owner-email"] = req.OwnerEmail
	}
	return attrs
}

func agentPoolUpdateAttrs(req UpdateAgentPoolRequest) map[string]any {
	attrs := map[string]any{}
	if req.Name != "" {
		attrs["name"] = req.Name
	}
	if req.Description != nil {
		attrs["description"] = *req.Description
	}
	if req.Labels != nil {
		attrs["labels"] = *req.Labels
	}
	if req.OwnerEmail != nil {
		attrs["owner-email"] = *req.OwnerEmail
	}
	return attrs
}

func parseAgentPool(body []byte) (*AgentPool, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse agent-pool response: %w", err)
	}
	return agentPoolFromResource(res), nil
}

func agentPoolFromResource(res *Resource) *AgentPool {
	p := &AgentPool{
		ID:          res.ID,
		Name:        GetStringAttr(res, "name"),
		Description: GetStringAttr(res, "description"),
		OwnerEmail:  GetStringAttr(res, "owner-email"),
		CreatedAt:   GetStringAttr(res, "created-at"),
		UpdatedAt:   GetStringAttr(res, "updated-at"),
	}
	if raw, ok := res.Attributes["labels"]; ok && len(raw) > 0 {
		var labels map[string]string
		if err := json.Unmarshal(raw, &labels); err == nil && len(labels) > 0 {
			p.Labels = labels
		}
	}
	return p
}
