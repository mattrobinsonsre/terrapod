package terrapod

import (
	"context"
	"fmt"
	"net/url"
)

// RemoteStateConsumer is an authorization edge — when set, the
// consumer workspace's agent runs may read the producer workspace's
// state via terraform_remote_state. Producer-controlled allowlist
// (#344): mutations require admin on the producer.
type RemoteStateConsumer struct {
	ID                    string `json:"id"`
	ProducerWorkspaceID   string `json:"producer-workspace-id"`
	ProducerWorkspaceName string `json:"producer-workspace-name"`
	ConsumerWorkspaceID   string `json:"consumer-workspace-id"`
	ConsumerWorkspaceName string `json:"consumer-workspace-name"`
	CreatedAt             string `json:"created-at,omitempty"`
	CreatedBy             string `json:"created-by,omitempty"`
}

// CreateRemoteStateConsumerRequest grants the consumer workspace
// read access to the producer's state. Caller must hold admin on
// the producer.
type CreateRemoteStateConsumerRequest struct {
	ProducerWorkspaceID string
	ConsumerWorkspaceID string
}

// CreateRemoteStateConsumer creates a grant edge.
func (c *Client) CreateRemoteStateConsumer(ctx context.Context, req CreateRemoteStateConsumerRequest) (*RemoteStateConsumer, error) {
	rels := map[string]any{
		"consumer": map[string]any{
			"data": map[string]any{
				"id":   req.ConsumerWorkspaceID,
				"type": "workspaces",
			},
		},
	}
	body, err := MarshalResource("remote-state-consumers", map[string]any{}, rels)
	if err != nil {
		return nil, fmt.Errorf("marshal create remote-state-consumer: %w", err)
	}
	data, err := c.Post(ctx,
		fmt.Sprintf("/api/terrapod/v1/workspaces/%s/remote-state-consumers", url.PathEscape(req.ProducerWorkspaceID)),
		body)
	if err != nil {
		return nil, err
	}
	return parseRemoteStateConsumer(data)
}

// GetRemoteStateConsumer reads an edge by id.
func (c *Client) GetRemoteStateConsumer(ctx context.Context, id string) (*RemoteStateConsumer, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/remote-state-consumers/"+url.PathEscape(id))
	if err != nil {
		return nil, err
	}
	return parseRemoteStateConsumer(data)
}

// ListRemoteStateConsumers returns the consumer grants for a
// producer workspace.
func (c *Client) ListRemoteStateConsumers(ctx context.Context, producerWorkspaceID string) ([]RemoteStateConsumer, error) {
	data, err := c.Get(ctx,
		fmt.Sprintf("/api/terrapod/v1/workspaces/%s/remote-state-consumers", url.PathEscape(producerWorkspaceID)))
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, err
	}
	out := make([]RemoteStateConsumer, 0, len(resources))
	for i := range resources {
		out = append(out, *remoteStateConsumerFromResource(&resources[i]))
	}
	return out, nil
}

// DeleteRemoteStateConsumer revokes the grant. Idempotent.
func (c *Client) DeleteRemoteStateConsumer(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/terrapod/v1/remote-state-consumers/"+url.PathEscape(id))
}

// ── Internal helpers ─────────────────────────────────────────────────

func parseRemoteStateConsumer(body []byte) (*RemoteStateConsumer, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse remote-state-consumer response: %w", err)
	}
	return remoteStateConsumerFromResource(res), nil
}

func remoteStateConsumerFromResource(res *Resource) *RemoteStateConsumer {
	rsc := &RemoteStateConsumer{
		ID:                    res.ID,
		ProducerWorkspaceName: GetStringAttr(res, "producer-workspace-name"),
		ConsumerWorkspaceName: GetStringAttr(res, "consumer-workspace-name"),
		CreatedAt:             GetStringAttr(res, "created-at"),
		CreatedBy:             GetStringAttr(res, "created-by"),
	}
	if v := GetRelationshipID(res, "producer"); v != "" {
		rsc.ProducerWorkspaceID = v
	}
	if v := GetRelationshipID(res, "consumer"); v != "" {
		rsc.ConsumerWorkspaceID = v
	}
	return rsc
}
