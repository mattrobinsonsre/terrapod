package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
)

// The labels browser is the read-only cross-entity label index served at
// /api/terrapod/v1/labels. All three endpoints are RBAC-filtered: results
// only include labels carried by entities the caller can read. Responses
// are plain JSON ({"data": ...}), not JSON:API resources.

// LabelKey is one distinct label key in use, with a count of distinct
// values and a per-entity-type breakdown of how many entities carry it.
type LabelKey struct {
	Key          string         `json:"key"`
	ValueCount   int            `json:"value-count"`
	EntityCounts map[string]int `json:"entity-counts"`
}

// LabelValue is one distinct value for a given key, with a per-entity-type
// count of how many entities carry key=value.
type LabelValue struct {
	Value        string         `json:"value"`
	EntityCounts map[string]int `json:"entity-counts"`
}

// LabelEntity is the minimal shape of an entity tagged with a label —
// enough to render a row and link to the entity's own page.
type LabelEntity struct {
	Type   string            `json:"type"`
	ID     string            `json:"id"`
	Name   string            `json:"name"`
	Labels map[string]string `json:"labels"`
}

// ListLabelKeys returns all label keys in use across readable entities.
func (c *Client) ListLabelKeys(ctx context.Context) ([]LabelKey, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/labels")
	if err != nil {
		return nil, err
	}
	var out struct {
		Data []LabelKey `json:"data"`
	}
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, fmt.Errorf("parse label keys: %w", err)
	}
	return out.Data, nil
}

// ListLabelValues returns the distinct values for a key. An empty slice is
// a valid response (no readable entity carries the key).
func (c *Client) ListLabelValues(ctx context.Context, key string) ([]LabelValue, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/labels/"+url.PathEscape(key))
	if err != nil {
		return nil, err
	}
	var out struct {
		Data []LabelValue `json:"data"`
	}
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, fmt.Errorf("parse label values: %w", err)
	}
	return out.Data, nil
}

// ListLabelEntities returns the entities tagged exactly key=value, grouped
// by entity type ("workspaces", "agent-pools", "registry-modules",
// "registry-providers"). Empty type lists are kept.
func (c *Client) ListLabelEntities(ctx context.Context, key, value string) (map[string][]LabelEntity, error) {
	path := fmt.Sprintf("/api/terrapod/v1/labels/%s/%s", url.PathEscape(key), url.PathEscape(value))
	data, err := c.Get(ctx, path)
	if err != nil {
		return nil, err
	}
	var out struct {
		Data map[string][]LabelEntity `json:"data"`
	}
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, fmt.Errorf("parse label entities: %w", err)
	}
	return out.Data, nil
}
