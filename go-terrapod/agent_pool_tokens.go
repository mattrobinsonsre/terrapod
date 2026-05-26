package terrapod

import (
	"context"
	"fmt"
	"net/url"
)

// AgentPoolToken is a join token for an agent pool — a listener
// uses it once at startup to exchange for an Ed25519 certificate.
// Token is the raw value, returned only at creation. Subsequent
// reads expose just the metadata (id, use_count, etc.).
type AgentPoolToken struct {
	ID          string `json:"id"`
	Token       string `json:"token,omitempty"` // raw value — create only
	Description string `json:"description,omitempty"`
	MaxUses     int64  `json:"max-uses,omitempty"`
	UseCount    int64  `json:"use-count"`
	IsRevoked   bool   `json:"is-revoked"`
	ExpiresAt   string `json:"expires-at,omitempty"`
	CreatedAt   string `json:"created-at,omitempty"`
	CreatedBy   string `json:"created-by,omitempty"`
}

// CreateAgentPoolTokenRequest is the input shape. MaxUses=0 means
// unlimited; ExpiresAt empty means no expiry.
type CreateAgentPoolTokenRequest struct {
	Description string
	MaxUses     int64
	ExpiresAt   string
}

// CreateAgentPoolToken issues a new join token. Pool admin required.
// The returned token's Token field contains the raw secret — store
// it immediately, the API will never return it again.
func (c *Client) CreateAgentPoolToken(ctx context.Context, poolID string, req CreateAgentPoolTokenRequest) (*AgentPoolToken, error) {
	attrs := map[string]any{}
	if req.Description != "" {
		attrs["description"] = req.Description
	}
	if req.MaxUses > 0 {
		attrs["max-uses"] = req.MaxUses
	}
	if req.ExpiresAt != "" {
		attrs["expires-at"] = req.ExpiresAt
	}
	body, err := MarshalResource("authentication-tokens", attrs, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create pool-token: %w", err)
	}
	data, err := c.Post(ctx,
		fmt.Sprintf("/api/terrapod/v1/agent-pools/%s/tokens", url.PathEscape(poolID)),
		body)
	if err != nil {
		return nil, err
	}
	return parseAgentPoolToken(data)
}

// ListAgentPoolTokens returns metadata for every token attached to
// the pool. The raw Token value is never present here.
func (c *Client) ListAgentPoolTokens(ctx context.Context, poolID string) ([]AgentPoolToken, error) {
	data, err := c.Get(ctx,
		fmt.Sprintf("/api/terrapod/v1/agent-pools/%s/tokens", url.PathEscape(poolID)))
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, err
	}
	out := make([]AgentPoolToken, 0, len(resources))
	for i := range resources {
		out = append(out, *agentPoolTokenFromResource(&resources[i]))
	}
	return out, nil
}

// GetAgentPoolToken fetches one token's metadata. The API doesn't
// expose a per-id GET — the SDK lists and filters. Returns
// nil + *NotFoundError when no token matches (consistent with
// every other Get* in this SDK).
func (c *Client) GetAgentPoolToken(ctx context.Context, poolID, tokenID string) (*AgentPoolToken, error) {
	tokens, err := c.ListAgentPoolTokens(ctx, poolID)
	if err != nil {
		return nil, err
	}
	for i := range tokens {
		if tokens[i].ID == tokenID {
			return &tokens[i], nil
		}
	}
	return nil, &NotFoundError{Resource: "agent-pool-token", ID: tokenID}
}

// DeleteAgentPoolToken revokes a token by id.
func (c *Client) DeleteAgentPoolToken(ctx context.Context, poolID, tokenID string) error {
	return c.Delete(ctx,
		fmt.Sprintf("/api/terrapod/v1/agent-pools/%s/tokens/%s",
			url.PathEscape(poolID), url.PathEscape(tokenID)))
}

// ── Internal helpers ─────────────────────────────────────────────────

func parseAgentPoolToken(body []byte) (*AgentPoolToken, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse pool-token response: %w", err)
	}
	return agentPoolTokenFromResource(res), nil
}

func agentPoolTokenFromResource(res *Resource) *AgentPoolToken {
	return &AgentPoolToken{
		ID:          res.ID,
		Token:       GetStringAttr(res, "token"),
		Description: GetStringAttr(res, "description"),
		MaxUses:     GetIntAttr(res, "max-uses"),
		UseCount:    GetIntAttr(res, "use-count"),
		IsRevoked:   GetBoolAttr(res, "is-revoked"),
		ExpiresAt:   GetStringAttr(res, "expires-at"),
		CreatedAt:   GetStringAttr(res, "created-at"),
		CreatedBy:   GetStringAttr(res, "created-by"),
	}
}
