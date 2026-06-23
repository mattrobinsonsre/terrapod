package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
)

// Service catalog (#535): provider templates, catalog items, and the
// provision/lifecycle flow over catalog instances. The whole surface lives
// under /api/terrapod/v1 and is gated server-side on catalog.enabled (404 when
// the feature is off). See docs/service-catalog.md.
//
// Catalog items and provider templates carry open-ended attribute sets
// (variable-options, parameters, provider-template-ids), so — like
// AutodiscoveryRule — the SDK exposes create/update payloads as raw
// map[string]any and returns the parsed server attributes for convenience.

// ProviderTemplate is an admin-managed, parameterised provider config rendered
// into a catalog instance's generated wrapper (providers.tf).
type ProviderTemplate struct {
	ID         string
	Attributes map[string]any
	CreatedAt  string
	UpdatedAt  string
}

// CatalogItem is a blessed designation over a registry module that users
// provision from without writing Terraform.
type CatalogItem struct {
	ID         string
	Attributes map[string]any
	CreatedAt  string
	UpdatedAt  string
}

// CatalogInstance is a provisioned workspace (catalog-managed). The ID is the
// workspace UUID.
type CatalogInstance struct {
	ID         string
	Attributes map[string]any
}

// CatalogRunRef is the minimal run reference returned by lifecycle actions
// (reconfigure / destroy).
type CatalogRunRef struct {
	ID        string
	Status    string
	IsDestroy bool
}

// ── Provider templates ─────────────────────────────────────────────────

// CreateProviderTemplate creates a provider template (admin only).
func (c *Client) CreateProviderTemplate(ctx context.Context, attrs map[string]any) (*ProviderTemplate, error) {
	body, err := MarshalResource("provider-templates", attrs, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create provider-template: %w", err)
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/provider-templates", body)
	if err != nil {
		return nil, err
	}
	return parseProviderTemplate(data)
}

// GetProviderTemplate reads a provider template by id.
func (c *Client) GetProviderTemplate(ctx context.Context, id string) (*ProviderTemplate, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/provider-templates/"+url.PathEscape(id))
	if err != nil {
		return nil, err
	}
	return parseProviderTemplate(data)
}

// ListProviderTemplates returns all provider templates (admin/audit).
func (c *Client) ListProviderTemplates(ctx context.Context) ([]ProviderTemplate, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/provider-templates")
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, err
	}
	out := make([]ProviderTemplate, 0, len(resources))
	for i := range resources {
		out = append(out, *providerTemplateFromResource(&resources[i]))
	}
	return out, nil
}

// UpdateProviderTemplate patches a provider template (admin only).
func (c *Client) UpdateProviderTemplate(ctx context.Context, id string, attrs map[string]any) (*ProviderTemplate, error) {
	body, err := MarshalResourceWithID(id, "provider-templates", attrs)
	if err != nil {
		return nil, fmt.Errorf("marshal update provider-template: %w", err)
	}
	data, err := c.Patch(ctx, "/api/terrapod/v1/provider-templates/"+url.PathEscape(id), body)
	if err != nil {
		return nil, err
	}
	return parseProviderTemplate(data)
}

// DeleteProviderTemplate removes a provider template (admin only). Returns a
// ConflictError if it is still referenced by a catalog item.
func (c *Client) DeleteProviderTemplate(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/terrapod/v1/provider-templates/"+url.PathEscape(id))
}

// ── Catalog items ──────────────────────────────────────────────────────

// CreateCatalogItem creates a catalog item (admin only).
func (c *Client) CreateCatalogItem(ctx context.Context, attrs map[string]any) (*CatalogItem, error) {
	body, err := MarshalResource("catalog-items", attrs, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create catalog-item: %w", err)
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/catalog-items", body)
	if err != nil {
		return nil, err
	}
	return parseCatalogItem(data)
}

// GetCatalogItem reads a catalog item by id (requires catalog read).
func (c *Client) GetCatalogItem(ctx context.Context, id string) (*CatalogItem, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/catalog-items/"+url.PathEscape(id))
	if err != nil {
		return nil, err
	}
	return parseCatalogItem(data)
}

// ListCatalogItems returns catalog items the caller has at least read on.
func (c *Client) ListCatalogItems(ctx context.Context) ([]CatalogItem, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/catalog-items")
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, err
	}
	out := make([]CatalogItem, 0, len(resources))
	for i := range resources {
		out = append(out, *catalogItemFromResource(&resources[i]))
	}
	return out, nil
}

// UpdateCatalogItem patches a catalog item (admin only).
func (c *Client) UpdateCatalogItem(ctx context.Context, id string, attrs map[string]any) (*CatalogItem, error) {
	body, err := MarshalResourceWithID(id, "catalog-items", attrs)
	if err != nil {
		return nil, fmt.Errorf("marshal update catalog-item: %w", err)
	}
	data, err := c.Patch(ctx, "/api/terrapod/v1/catalog-items/"+url.PathEscape(id), body)
	if err != nil {
		return nil, err
	}
	return parseCatalogItem(data)
}

// DeleteCatalogItem removes a catalog item (admin only). Returns a
// ConflictError if the item still has provisioned instances.
func (c *Client) DeleteCatalogItem(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/terrapod/v1/catalog-items/"+url.PathEscape(id))
}

// GetCatalogItemForm returns the provision form (the fields a user fills in)
// for a catalog item, along with the resolved module version. The returned map
// is the raw `attributes` object: {"resolved-version": ..., "fields": [...]}.
func (c *Client) GetCatalogItemForm(ctx context.Context, id string) (map[string]any, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/catalog-items/"+url.PathEscape(id)+"/form")
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse catalog-item form: %w", err)
	}
	return attrsToMap(res), nil
}

// ── Catalog instances (provision + lifecycle) ──────────────────────────

// ProvisionCatalogItem provisions a new instance from a catalog item. Requires
// catalog 'use' on the item and 'write' on the chosen agent pool.
func (c *Client) ProvisionCatalogItem(ctx context.Context, itemID string, attrs map[string]any) (*CatalogInstance, error) {
	body, err := MarshalResource("catalog-instances", attrs, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal provision: %w", err)
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/catalog-items/"+url.PathEscape(itemID)+"/provision", body)
	if err != nil {
		return nil, err
	}
	return parseCatalogInstance(data)
}

// ListCatalogInstances returns the provisioned instances of a catalog item.
func (c *Client) ListCatalogInstances(ctx context.Context, itemID string) ([]CatalogInstance, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/catalog-items/"+url.PathEscape(itemID)+"/instances")
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, err
	}
	out := make([]CatalogInstance, 0, len(resources))
	for i := range resources {
		out = append(out, *catalogInstanceFromResource(&resources[i]))
	}
	return out, nil
}

// GetCatalogInstance reads a single catalog instance by workspace id (requires
// catalog read on the originating item).
func (c *Client) GetCatalogInstance(ctx context.Context, wsID string) (*CatalogInstance, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/catalog-instances/"+url.PathEscape(wsID))
	if err != nil {
		return nil, err
	}
	return parseCatalogInstance(data)
}

// ReconfigureCatalogInstance updates an instance's inputs and/or version pin
// and queues a run. Requires catalog 'use' on the originating item. Returns the
// queued run reference.
func (c *Client) ReconfigureCatalogInstance(ctx context.Context, wsID string, attrs map[string]any) (*CatalogRunRef, error) {
	body, err := MarshalResource("catalog-instances", attrs, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal reconfigure: %w", err)
	}
	data, err := c.Patch(ctx, "/api/terrapod/v1/catalog-instances/"+url.PathEscape(wsID), body)
	if err != nil {
		return nil, err
	}
	return parseCatalogRunRef(data)
}

// DestroyCatalogInstance queues a destroy run for an instance. On a successful
// apply the workspace is archived. Requires catalog 'use' on the originating
// item. Returns the queued run reference.
func (c *Client) DestroyCatalogInstance(ctx context.Context, wsID string, attrs map[string]any) (*CatalogRunRef, error) {
	body, err := MarshalResource("catalog-instances", attrs, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal destroy: %w", err)
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/catalog-instances/"+url.PathEscape(wsID)+"/destroy", body)
	if err != nil {
		return nil, err
	}
	return parseCatalogRunRef(data)
}

// ConfirmCatalogInstanceRun confirms the instance's pending planned run for
// apply. This is the catalog-surface confirm — the workspace clamp gives the
// provisioner only read, so a non-auto-apply provision/reconfigure/destroy is
// confirmed here rather than via the workspace run API. Requires catalog 'use'.
func (c *Client) ConfirmCatalogInstanceRun(ctx context.Context, wsID string) (*CatalogRunRef, error) {
	data, err := c.Post(ctx, "/api/terrapod/v1/catalog-instances/"+url.PathEscape(wsID)+"/confirm", nil)
	if err != nil {
		return nil, err
	}
	return parseCatalogRunRef(data)
}

// DiscardCatalogInstanceRun discards the instance's pending planned run.
// Catalog-surface counterpart of confirm. Requires catalog 'use'.
func (c *Client) DiscardCatalogInstanceRun(ctx context.Context, wsID string) (*CatalogRunRef, error) {
	data, err := c.Post(ctx, "/api/terrapod/v1/catalog-instances/"+url.PathEscape(wsID)+"/discard", nil)
	if err != nil {
		return nil, err
	}
	return parseCatalogRunRef(data)
}

// OrphanCatalogInstance deletes a catalog instance's workspace record WITHOUT
// destroying its infrastructure — the provisioned resources keep running,
// untracked. This is the explicit, discouraged escape hatch; the recommended
// teardown is DestroyCatalogInstance, which reclaims the infrastructure.
// Requires catalog 'admin' on the originating item.
func (c *Client) OrphanCatalogInstance(ctx context.Context, wsID string) error {
	return c.Delete(ctx, "/api/terrapod/v1/catalog-instances/"+url.PathEscape(wsID)+"?orphan=true")
}

// ── Internal helpers ─────────────────────────────────────────────────

func attrsToMap(res *Resource) map[string]any {
	out := map[string]any{}
	for k, raw := range res.Attributes {
		var v any
		if err := json.Unmarshal(raw, &v); err == nil {
			out[k] = v
		}
	}
	return out
}

func providerTemplateFromResource(res *Resource) *ProviderTemplate {
	return &ProviderTemplate{
		ID:         res.ID,
		Attributes: attrsToMap(res),
		CreatedAt:  GetStringAttr(res, "created-at"),
		UpdatedAt:  GetStringAttr(res, "updated-at"),
	}
}

func parseProviderTemplate(body []byte) (*ProviderTemplate, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse provider-template response: %w", err)
	}
	return providerTemplateFromResource(res), nil
}

func catalogItemFromResource(res *Resource) *CatalogItem {
	return &CatalogItem{
		ID:         res.ID,
		Attributes: attrsToMap(res),
		CreatedAt:  GetStringAttr(res, "created-at"),
		UpdatedAt:  GetStringAttr(res, "updated-at"),
	}
}

func parseCatalogItem(body []byte) (*CatalogItem, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse catalog-item response: %w", err)
	}
	return catalogItemFromResource(res), nil
}

func catalogInstanceFromResource(res *Resource) *CatalogInstance {
	return &CatalogInstance{ID: res.ID, Attributes: attrsToMap(res)}
}

func parseCatalogInstance(body []byte) (*CatalogInstance, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse catalog-instance response: %w", err)
	}
	return catalogInstanceFromResource(res), nil
}

func parseCatalogRunRef(body []byte) (*CatalogRunRef, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse catalog run reference: %w", err)
	}
	return &CatalogRunRef{
		ID:        res.ID,
		Status:    GetStringAttr(res, "status"),
		IsDestroy: GetBoolAttr(res, "is-destroy"),
	}, nil
}
