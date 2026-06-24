package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
)

// Role is the decoded form of a Terrapod custom RBAC role. The role
// name is the stable identifier; it appears at the top of the
// JSON:API "data" object rather than inside attributes (this is a
// Terrapod-specific quirk — historical from before the role
// management API was tidied).
//
// AllowLabels/AllowNames grant the role's workspace_permission to
// matching workspaces; DenyLabels/DenyNames override. Resolution
// rules are documented in the platform CLAUDE.md.
type Role struct {
	Name        string `json:"name"`
	Description string `json:"description,omitempty"`

	AllowLabels map[string]string `json:"allow-labels,omitempty"`
	AllowNames  []string          `json:"allow-names,omitempty"`
	DenyLabels  map[string]string `json:"deny-labels,omitempty"`
	DenyNames   []string          `json:"deny-names,omitempty"`

	WorkspacePermission string `json:"workspace-permission"` // read | plan | write | admin
	PoolPermission      string `json:"pool-permission,omitempty"`
	RegistryPermission  string `json:"registry-permission,omitempty"` // read | write | admin (modules + providers)
	CatalogPermission   string `json:"catalog-permission,omitempty"`  // none | read | use | admin

	BuiltIn   bool   `json:"built-in"`
	CreatedAt string `json:"created-at,omitempty"`
	UpdatedAt string `json:"updated-at,omitempty"`
}

// CreateRoleRequest is the input shape for Client.CreateRole.
// WorkspacePermission is required. PoolPermission defaults to "read"
// when empty (server side). Allow/Deny fields are independent — an
// empty allow list means the role doesn't grant access by label/name
// match (use the workspace owner field for that case instead).
type CreateRoleRequest struct {
	Name        string
	Description string

	AllowLabels map[string]string
	AllowNames  []string
	DenyLabels  map[string]string
	DenyNames   []string

	WorkspacePermission string
	PoolPermission      string
	RegistryPermission  string
	CatalogPermission   string
}

// UpdateRoleRequest is the partial-update shape. Name is immutable.
// Pointer fields preserve "leave alone" semantics so a vanilla rename
// doesn't clear allow/deny sets. Pass a pointer to an empty value to
// explicitly clear.
type UpdateRoleRequest struct {
	Description *string

	AllowLabels *map[string]string
	AllowNames  *[]string
	DenyLabels  *map[string]string
	DenyNames   *[]string

	WorkspacePermission string
	PoolPermission      string
	RegistryPermission  string
	CatalogPermission   string
}

// CreateRole creates a custom role. Admin required.
func (c *Client) CreateRole(ctx context.Context, req CreateRoleRequest) (*Role, error) {
	body, err := marshalRoleDoc(req.Name, roleCreateAttrs(req))
	if err != nil {
		return nil, fmt.Errorf("marshal create role: %w", err)
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/roles", body)
	if err != nil {
		return nil, err
	}
	return parseRole(data)
}

// GetRole reads a role by name.
func (c *Client) GetRole(ctx context.Context, name string) (*Role, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/roles/"+url.PathEscape(name))
	if err != nil {
		return nil, err
	}
	return parseRole(data)
}

// ListRoles returns every role (admin/audit see all). Built-in roles
// are included; check Role.BuiltIn before attempting to modify.
func (c *Client) ListRoles(ctx context.Context) ([]Role, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/roles")
	if err != nil {
		return nil, err
	}
	return parseRoleList(data)
}

// UpdateRole patches a custom role. Built-in roles cannot be edited —
// the server returns 422.
func (c *Client) UpdateRole(ctx context.Context, name string, req UpdateRoleRequest) (*Role, error) {
	body, err := marshalRoleDoc(name, roleUpdateAttrs(req))
	if err != nil {
		return nil, fmt.Errorf("marshal update role: %w", err)
	}
	data, err := c.Patch(ctx, "/api/terrapod/v1/roles/"+url.PathEscape(name), body)
	if err != nil {
		return nil, err
	}
	return parseRole(data)
}

// DeleteRole removes a custom role. Built-in roles cannot be deleted.
// Existing role_assignments for the role are also removed.
func (c *Client) DeleteRole(ctx context.Context, name string) error {
	return c.Delete(ctx, "/api/terrapod/v1/roles/"+url.PathEscape(name))
}

// ── Internal helpers ─────────────────────────────────────────────────

func roleCreateAttrs(req CreateRoleRequest) map[string]any {
	attrs := map[string]any{
		"workspace-permission": req.WorkspacePermission,
	}
	if req.PoolPermission != "" {
		attrs["pool-permission"] = req.PoolPermission
	}
	if req.RegistryPermission != "" {
		attrs["registry-permission"] = req.RegistryPermission
	}
	if req.CatalogPermission != "" {
		attrs["catalog-permission"] = req.CatalogPermission
	}
	if req.Description != "" {
		attrs["description"] = req.Description
	}
	// Always send allow/deny fields so the server uses the supplied
	// values verbatim (empty slice/map = no allow rules, not "leave alone").
	attrs["allow-labels"] = mapOrEmpty(req.AllowLabels)
	attrs["allow-names"] = sliceOrEmpty(req.AllowNames)
	attrs["deny-labels"] = mapOrEmpty(req.DenyLabels)
	attrs["deny-names"] = sliceOrEmpty(req.DenyNames)
	return attrs
}

func roleUpdateAttrs(req UpdateRoleRequest) map[string]any {
	attrs := map[string]any{}
	if req.WorkspacePermission != "" {
		attrs["workspace-permission"] = req.WorkspacePermission
	}
	if req.PoolPermission != "" {
		attrs["pool-permission"] = req.PoolPermission
	}
	if req.RegistryPermission != "" {
		attrs["registry-permission"] = req.RegistryPermission
	}
	if req.CatalogPermission != "" {
		attrs["catalog-permission"] = req.CatalogPermission
	}
	if req.Description != nil {
		attrs["description"] = *req.Description
	}
	if req.AllowLabels != nil {
		attrs["allow-labels"] = *req.AllowLabels
	}
	if req.AllowNames != nil {
		attrs["allow-names"] = *req.AllowNames
	}
	if req.DenyLabels != nil {
		attrs["deny-labels"] = *req.DenyLabels
	}
	if req.DenyNames != nil {
		attrs["deny-names"] = *req.DenyNames
	}
	return attrs
}

func mapOrEmpty(m map[string]string) map[string]string {
	if m == nil {
		return map[string]string{}
	}
	return m
}

func sliceOrEmpty(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}

// marshalRoleDoc builds the JSON:API body for role create/update.
// The roles endpoint uses "name" at the data level (no "id").
func marshalRoleDoc(name string, attributes map[string]any) ([]byte, error) {
	return json.Marshal(map[string]any{
		"data": map[string]any{
			"name":       name,
			"type":       "roles",
			"attributes": attributes,
		},
	})
}

// roleDataEnvelope captures the role-specific JSON:API shape (name
// at the data level rather than id).
type roleDataEnvelope struct {
	Data roleDataItem `json:"data"`
}

type roleDataListEnvelope struct {
	Data []roleDataItem `json:"data"`
}

type roleDataItem struct {
	Name       string          `json:"name"`
	Type       string          `json:"type"`
	Attributes json.RawMessage `json:"attributes"`
}

func parseRole(body []byte) (*Role, error) {
	var doc roleDataEnvelope
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, fmt.Errorf("parse role response: %w", err)
	}
	return roleFromItem(&doc.Data)
}

func parseRoleList(body []byte) ([]Role, error) {
	var doc roleDataListEnvelope
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, fmt.Errorf("parse role list: %w", err)
	}
	out := make([]Role, 0, len(doc.Data))
	for i := range doc.Data {
		r, err := roleFromItem(&doc.Data[i])
		if err != nil {
			return nil, err
		}
		out = append(out, *r)
	}
	return out, nil
}

func roleFromItem(item *roleDataItem) (*Role, error) {
	var attrs struct {
		Description         string            `json:"description"`
		AllowLabels         map[string]string `json:"allow-labels"`
		AllowNames          []string          `json:"allow-names"`
		DenyLabels          map[string]string `json:"deny-labels"`
		DenyNames           []string          `json:"deny-names"`
		WorkspacePermission string            `json:"workspace-permission"`
		PoolPermission      string            `json:"pool-permission"`
		RegistryPermission  string            `json:"registry-permission"`
		CatalogPermission   string            `json:"catalog-permission"`
		BuiltIn             bool              `json:"built-in"`
		CreatedAt           string            `json:"created-at"`
		UpdatedAt           string            `json:"updated-at"`
	}
	if len(item.Attributes) > 0 {
		if err := json.Unmarshal(item.Attributes, &attrs); err != nil {
			return nil, fmt.Errorf("parse role attributes: %w", err)
		}
	}
	return &Role{
		Name:                item.Name,
		Description:         attrs.Description,
		AllowLabels:         attrs.AllowLabels,
		AllowNames:          attrs.AllowNames,
		DenyLabels:          attrs.DenyLabels,
		DenyNames:           attrs.DenyNames,
		WorkspacePermission: attrs.WorkspacePermission,
		PoolPermission:      attrs.PoolPermission,
		RegistryPermission:  attrs.RegistryPermission,
		CatalogPermission:   attrs.CatalogPermission,
		BuiltIn:             attrs.BuiltIn,
		CreatedAt:           attrs.CreatedAt,
		UpdatedAt:           attrs.UpdatedAt,
	}, nil
}
