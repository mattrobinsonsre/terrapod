package terrapod

import (
	"context"
	"fmt"
	"net/url"
)

// ModuleWorkspaceLink connects a registry module to a workspace that
// consumes it. The link enables impact-analysis: when the module's
// VCS repo receives a PR, speculative plan-only runs are queued on
// linked workspaces (using the PR's HEAD tarball as a module override).
type ModuleWorkspaceLink struct {
	ID            string `json:"id"`
	WorkspaceID   string `json:"workspace-id"`
	WorkspaceName string `json:"workspace-name,omitempty"`
	CreatedAt     string `json:"created-at,omitempty"`
	CreatedBy     string `json:"created-by,omitempty"`
}

// CreateModuleWorkspaceLinkRequest links a workspace to a module. The
// module is identified by (name, provider) — the namespace is always
// `default` (single-org).
type CreateModuleWorkspaceLinkRequest struct {
	ModuleName     string
	ModuleProvider string
	WorkspaceID    string
}

// CreateModuleWorkspaceLink creates the link. Caller needs write+
// on the workspace AND read+ on the module.
func (c *Client) CreateModuleWorkspaceLink(ctx context.Context, req CreateModuleWorkspaceLinkRequest) (*ModuleWorkspaceLink, error) {
	body, err := MarshalResource("workspace-links", map[string]any{
		"workspace_id": req.WorkspaceID,
	}, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create workspace-link: %w", err)
	}
	data, err := c.Post(ctx, moduleLinksPath(req.ModuleName, req.ModuleProvider), body)
	if err != nil {
		return nil, err
	}
	return parseModuleWorkspaceLink(data)
}

// ListModuleWorkspaceLinks returns every workspace linked to the
// (name, provider) module.
func (c *Client) ListModuleWorkspaceLinks(ctx context.Context, moduleName, moduleProvider string) ([]ModuleWorkspaceLink, error) {
	data, err := c.Get(ctx, moduleLinksPath(moduleName, moduleProvider))
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, err
	}
	out := make([]ModuleWorkspaceLink, 0, len(resources))
	for i := range resources {
		out = append(out, *moduleWorkspaceLinkFromResource(&resources[i]))
	}
	return out, nil
}

// GetModuleWorkspaceLink looks up a single link by id. Filters
// client-side from ListModuleWorkspaceLinks because the server
// doesn't expose a single-link GET (the link id is composite per-
// module). Returns nil + *NotFoundError when no link matches.
func (c *Client) GetModuleWorkspaceLink(ctx context.Context, moduleName, moduleProvider, linkID string) (*ModuleWorkspaceLink, error) {
	links, err := c.ListModuleWorkspaceLinks(ctx, moduleName, moduleProvider)
	if err != nil {
		return nil, err
	}
	for i := range links {
		if links[i].ID == linkID {
			return &links[i], nil
		}
	}
	return nil, &NotFoundError{Resource: "module-workspace-link", ID: linkID}
}

// DeleteModuleWorkspaceLink removes the link by id.
func (c *Client) DeleteModuleWorkspaceLink(ctx context.Context, moduleName, moduleProvider, linkID string) error {
	return c.Delete(ctx, moduleLinksPath(moduleName, moduleProvider)+"/"+url.PathEscape(linkID))
}

// ── Internal helpers ─────────────────────────────────────────────────

func moduleLinksPath(name, provider string) string {
	return fmt.Sprintf("/api/terrapod/v1/registry-modules/private/default/%s/%s/workspace-links",
		url.PathEscape(name), url.PathEscape(provider))
}

func parseModuleWorkspaceLink(body []byte) (*ModuleWorkspaceLink, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse workspace-link response: %w", err)
	}
	return moduleWorkspaceLinkFromResource(res), nil
}

func moduleWorkspaceLinkFromResource(res *Resource) *ModuleWorkspaceLink {
	return &ModuleWorkspaceLink{
		ID:            res.ID,
		WorkspaceID:   GetStringAttr(res, "workspace-id"),
		WorkspaceName: GetStringAttr(res, "workspace-name"),
		CreatedAt:     GetStringAttr(res, "created-at"),
		CreatedBy:     GetStringAttr(res, "created-by"),
	}
}
