package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
)

// AutodiscoveryRule is the platform-level config that auto-creates
// workspaces when a PR or default-branch push touches a path matching
// the rule's pattern. See terrapod #283 / docs/autodiscovery.md.
//
// The set of template fields (run-task-templates, notification-
// templates, etc.) is open-ended on the API side, so the SDK exposes
// the create/update payload as raw map[string]any. The Attributes
// field on the result is the parsed server response, so callers can
// read computed fields like created-at/updated-at without re-parsing
// the underlying JSON:API document.
type AutodiscoveryRule struct {
	ID         string
	Attributes map[string]any
	CreatedAt  string
	UpdatedAt  string
}

// CreateAutodiscoveryRule creates a new rule with the given JSON:API
// attribute map. The map keys are the hyphenated wire names — same
// shape as the server stores. The SDK doesn't validate the contents;
// the server returns 422 on invalid attribute names.
func (c *Client) CreateAutodiscoveryRule(ctx context.Context, attrs map[string]any) (*AutodiscoveryRule, error) {
	body, err := MarshalResource("autodiscovery-rules", attrs, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create autodiscovery-rule: %w", err)
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/autodiscovery-rules", body)
	if err != nil {
		return nil, err
	}
	return parseAutodiscoveryRule(data)
}

// GetAutodiscoveryRule reads a rule by id.
func (c *Client) GetAutodiscoveryRule(ctx context.Context, id string) (*AutodiscoveryRule, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/autodiscovery-rules/"+url.PathEscape(id))
	if err != nil {
		return nil, err
	}
	return parseAutodiscoveryRule(data)
}

// UpdateAutodiscoveryRule patches a rule. Attribute map semantics
// mirror create: attributes present in the map are updated, absent
// keys are left alone.
func (c *Client) UpdateAutodiscoveryRule(ctx context.Context, id string, attrs map[string]any) (*AutodiscoveryRule, error) {
	body, err := MarshalResourceWithID(id, "autodiscovery-rules", attrs)
	if err != nil {
		return nil, fmt.Errorf("marshal update autodiscovery-rule: %w", err)
	}
	data, err := c.Patch(ctx, "/api/terrapod/v1/autodiscovery-rules/"+url.PathEscape(id), body)
	if err != nil {
		return nil, err
	}
	return parseAutodiscoveryRule(data)
}

// DeleteAutodiscoveryRule removes a rule. Workspaces already
// autodiscovered from this rule continue to exist; they only lose
// the link back to the source rule.
func (c *Client) DeleteAutodiscoveryRule(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/terrapod/v1/autodiscovery-rules/"+url.PathEscape(id))
}

// ── Internal helpers ─────────────────────────────────────────────────

func parseAutodiscoveryRule(body []byte) (*AutodiscoveryRule, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse autodiscovery-rule response: %w", err)
	}
	rule := &AutodiscoveryRule{
		ID:         res.ID,
		Attributes: map[string]any{},
		CreatedAt:  GetStringAttr(res, "created-at"),
		UpdatedAt:  GetStringAttr(res, "updated-at"),
	}
	for k, raw := range res.Attributes {
		var v any
		if err := json.Unmarshal(raw, &v); err == nil {
			rule.Attributes[k] = v
		}
	}
	return rule, nil
}
