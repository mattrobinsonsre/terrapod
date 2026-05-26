package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
)

// RegistryProvider is a private Terraform provider in the Terrapod
// registry. Versions and platform binaries are managed via separate
// endpoints not yet exposed by the SDK (upload still goes through
// the legacy raw client).
type RegistryProvider struct {
	ID         string            `json:"id"`
	Name       string            `json:"name"`
	Namespace  string            `json:"namespace,omitempty"`
	Labels     map[string]string `json:"labels,omitempty"`
	OwnerEmail string            `json:"owner-email,omitempty"`
	CreatedAt  string            `json:"created-at,omitempty"`
	UpdatedAt  string            `json:"updated-at,omitempty"`
}

// CreateRegistryProviderRequest registers a new provider.
type CreateRegistryProviderRequest struct {
	Name   string
	Labels map[string]string
}

// UpdateRegistryProviderRequest patches a provider. Name is immutable
// (passed as path component).
type UpdateRegistryProviderRequest struct {
	Labels *map[string]string
}

// CreateRegistryProvider creates a provider entry.
func (c *Client) CreateRegistryProvider(ctx context.Context, req CreateRegistryProviderRequest) (*RegistryProvider, error) {
	attrs := map[string]any{"name": req.Name}
	if req.Labels != nil {
		attrs["labels"] = req.Labels
	}
	body, err := MarshalResource("registry-providers", attrs, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create registry-provider: %w", err)
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/registry-providers", body)
	if err != nil {
		return nil, err
	}
	return parseRegistryProvider(data)
}

// GetRegistryProvider reads a provider by name.
func (c *Client) GetRegistryProvider(ctx context.Context, name string) (*RegistryProvider, error) {
	data, err := c.Get(ctx, registryProviderPath(name))
	if err != nil {
		return nil, err
	}
	return parseRegistryProvider(data)
}

// UpdateRegistryProvider patches a provider by name.
func (c *Client) UpdateRegistryProvider(ctx context.Context, name string, req UpdateRegistryProviderRequest) (*RegistryProvider, error) {
	attrs := map[string]any{}
	if req.Labels != nil {
		attrs["labels"] = *req.Labels
	}
	body, err := MarshalResource("registry-providers", attrs, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal update registry-provider: %w", err)
	}
	data, err := c.Patch(ctx, registryProviderPath(name), body)
	if err != nil {
		return nil, err
	}
	return parseRegistryProvider(data)
}

// DeleteRegistryProvider removes a provider entry. Versions and
// platform binaries are cascade-deleted from storage.
func (c *Client) DeleteRegistryProvider(ctx context.Context, name string) error {
	return c.Delete(ctx, registryProviderPath(name))
}

// ── Internal helpers ─────────────────────────────────────────────────

func registryProviderPath(name string) string {
	return fmt.Sprintf("/api/terrapod/v1/registry-providers/private/default/%s", url.PathEscape(name))
}

func parseRegistryProvider(body []byte) (*RegistryProvider, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse registry-provider response: %w", err)
	}
	return registryProviderFromResource(res), nil
}

func registryProviderFromResource(res *Resource) *RegistryProvider {
	p := &RegistryProvider{
		ID:         res.ID,
		Name:       GetStringAttr(res, "name"),
		Namespace:  GetStringAttr(res, "namespace"),
		OwnerEmail: GetStringAttr(res, "owner-email"),
		CreatedAt:  GetStringAttr(res, "created-at"),
		UpdatedAt:  GetStringAttr(res, "updated-at"),
	}
	if raw, ok := res.Attributes["labels"]; ok && len(raw) > 0 {
		var labels map[string]string
		if err := json.Unmarshal(raw, &labels); err == nil && len(labels) > 0 {
			p.Labels = labels
		}
	}
	return p
}
