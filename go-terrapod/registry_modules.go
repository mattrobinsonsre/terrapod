package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
)

// RegistryModule is a private module in the Terrapod registry.
// Source is "upload" for tarball-based modules and "vcs" for modules
// that auto-publish on tags. Status reports cleanly-published vs
// errored. The module is keyed by (name, provider_name) within the
// single `default` namespace.
type RegistryModule struct {
	ID              string            `json:"id"`
	Name            string            `json:"name"`
	ProviderName    string            `json:"provider"`
	Namespace       string            `json:"namespace,omitempty"`
	Labels          map[string]string `json:"labels,omitempty"`
	VCSConnectionID string            `json:"vcs-connection-id,omitempty"`
	VCSRepoURL      string            `json:"vcs-repo-url,omitempty"`
	VCSBranch       string            `json:"vcs-branch,omitempty"`
	VCSTagPattern   string            `json:"vcs-tag-pattern,omitempty"`
	Status          string            `json:"status,omitempty"`
	OwnerEmail      string            `json:"owner-email,omitempty"`
	Source          string            `json:"source,omitempty"`
	CreatedAt       string            `json:"created-at,omitempty"`
	UpdatedAt       string            `json:"updated-at,omitempty"`
}

// ModuleInterface is a published module version's input/output surface —
// what an agent needs to author a correct `module` block against it.
type ModuleInterface struct {
	Version string           `json:"version"`
	Inputs  []map[string]any `json:"inputs"`
	Outputs []map[string]any `json:"outputs"`
}

// CreateRegistryModuleRequest registers a new module.
type CreateRegistryModuleRequest struct {
	Name            string
	ProviderName    string
	Labels          map[string]string
	VCSConnectionID string
	VCSRepoURL      string
	VCSBranch       string
	VCSTagPattern   string
}

// UpdateRegistryModuleRequest patches a module. Name and provider
// are immutable on the API side; pass them only as path components.
// Pointer fields preserve "leave alone" semantics.
type UpdateRegistryModuleRequest struct {
	Labels          *map[string]string
	VCSConnectionID *string
	VCSRepoURL      *string
	VCSBranch       *string
	VCSTagPattern   *string
}

// CreateRegistryModule creates a module. Caller becomes owner.
func (c *Client) CreateRegistryModule(ctx context.Context, req CreateRegistryModuleRequest) (*RegistryModule, error) {
	body, err := MarshalResource("registry-modules", regModuleCreateAttrs(req), nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create registry-module: %w", err)
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/registry-modules", body)
	if err != nil {
		return nil, err
	}
	return parseRegistryModule(data)
}

// GetRegistryModule reads a module by (name, provider).
func (c *Client) GetRegistryModule(ctx context.Context, name, providerName string) (*RegistryModule, error) {
	data, err := c.Get(ctx, registryModulePath(name, providerName))
	if err != nil {
		return nil, err
	}
	return parseRegistryModule(data)
}

// ListRegistryModules returns every module in the registry (the
// management list — not the CLI download protocol). Slices the
// server's JSON:API list through the same per-resource projector the
// single-resource Get uses.
func (c *Client) ListRegistryModules(ctx context.Context) ([]RegistryModule, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/registry-modules")
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, err
	}
	modules := make([]RegistryModule, 0, len(resources))
	for i := range resources {
		modules = append(modules, *registryModuleFromResource(&resources[i]))
	}
	return modules, nil
}

// GetModuleInterface reads a published module version's input/output
// surface — the inputs an agent must supply and the outputs it can
// consume when authoring a `module` block. Returns *NotFoundError when
// interface extraction is disabled for the module or the version is
// unknown.
func (c *Client) GetModuleInterface(ctx context.Context, name, provider, version string) (*ModuleInterface, error) {
	path := fmt.Sprintf("/api/terrapod/v1/registry-modules/private/default/%s/%s/%s/interface",
		url.PathEscape(name), url.PathEscape(provider), url.PathEscape(version))
	data, err := c.Get(ctx, path)
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse module-interface response: %w", err)
	}
	iface := &ModuleInterface{Version: GetStringAttr(res, "version")}
	if raw, ok := res.Attributes["inputs"]; ok && len(raw) > 0 && string(raw) != "null" {
		if err := json.Unmarshal(raw, &iface.Inputs); err != nil {
			return nil, fmt.Errorf("parse module-interface inputs: %w", err)
		}
	}
	if raw, ok := res.Attributes["outputs"]; ok && len(raw) > 0 && string(raw) != "null" {
		if err := json.Unmarshal(raw, &iface.Outputs); err != nil {
			return nil, fmt.Errorf("parse module-interface outputs: %w", err)
		}
	}
	return iface, nil
}

// UpdateRegistryModule patches a module identified by (name, provider).
func (c *Client) UpdateRegistryModule(ctx context.Context, name, providerName string, req UpdateRegistryModuleRequest) (*RegistryModule, error) {
	body, err := MarshalResource("registry-modules", regModuleUpdateAttrs(req), nil)
	if err != nil {
		return nil, fmt.Errorf("marshal update registry-module: %w", err)
	}
	data, err := c.Patch(ctx, registryModulePath(name, providerName), body)
	if err != nil {
		return nil, err
	}
	return parseRegistryModule(data)
}

// DeleteRegistryModule removes a module. Cascade-deletes versions,
// workspace links, and storage tarballs.
func (c *Client) DeleteRegistryModule(ctx context.Context, name, providerName string) error {
	return c.Delete(ctx, registryModulePath(name, providerName))
}

// ── Internal helpers ─────────────────────────────────────────────────

func registryModulePath(name, provider string) string {
	return fmt.Sprintf("/api/terrapod/v1/registry-modules/private/default/%s/%s",
		url.PathEscape(name), url.PathEscape(provider))
}

func regModuleCreateAttrs(req CreateRegistryModuleRequest) map[string]any {
	attrs := map[string]any{
		"name":     req.Name,
		"provider": req.ProviderName,
	}
	if req.Labels != nil {
		attrs["labels"] = req.Labels
	}
	if req.VCSConnectionID != "" {
		attrs["vcs-connection-id"] = req.VCSConnectionID
	}
	if req.VCSRepoURL != "" {
		attrs["vcs-repo-url"] = req.VCSRepoURL
	}
	if req.VCSBranch != "" {
		attrs["vcs-branch"] = req.VCSBranch
	}
	if req.VCSTagPattern != "" {
		attrs["vcs-tag-pattern"] = req.VCSTagPattern
	}
	return attrs
}

func regModuleUpdateAttrs(req UpdateRegistryModuleRequest) map[string]any {
	attrs := map[string]any{}
	if req.Labels != nil {
		attrs["labels"] = *req.Labels
	}
	if req.VCSConnectionID != nil {
		attrs["vcs-connection-id"] = *req.VCSConnectionID
	}
	if req.VCSRepoURL != nil {
		attrs["vcs-repo-url"] = *req.VCSRepoURL
	}
	if req.VCSBranch != nil {
		attrs["vcs-branch"] = *req.VCSBranch
	}
	if req.VCSTagPattern != nil {
		attrs["vcs-tag-pattern"] = *req.VCSTagPattern
	}
	return attrs
}

func parseRegistryModule(body []byte) (*RegistryModule, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse registry-module response: %w", err)
	}
	return registryModuleFromResource(res), nil
}

func registryModuleFromResource(res *Resource) *RegistryModule {
	m := &RegistryModule{
		ID:              res.ID,
		Name:            GetStringAttr(res, "name"),
		ProviderName:    GetStringAttr(res, "provider"),
		Namespace:       GetStringAttr(res, "namespace"),
		VCSConnectionID: GetStringAttr(res, "vcs-connection-id"),
		VCSRepoURL:      GetStringAttr(res, "vcs-repo-url"),
		VCSBranch:       GetStringAttr(res, "vcs-branch"),
		VCSTagPattern:   GetStringAttr(res, "vcs-tag-pattern"),
		Status:          GetStringAttr(res, "status"),
		OwnerEmail:      GetStringAttr(res, "owner-email"),
		Source:          GetStringAttr(res, "source"),
		CreatedAt:       GetStringAttr(res, "created-at"),
		UpdatedAt:       GetStringAttr(res, "updated-at"),
	}
	if raw, ok := res.Attributes["labels"]; ok && len(raw) > 0 {
		var labels map[string]string
		if err := json.Unmarshal(raw, &labels); err == nil && len(labels) > 0 {
			m.Labels = labels
		}
	}
	return m
}
