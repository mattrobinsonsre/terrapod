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
// AllowLabels/AllowNames scope the role's Capabilities to matching
// workspaces (and pools/registry/catalog items); DenyLabels/DenyNames
// override. Capabilities are the grant (see the Capabilities field);
// resolution rules are documented in docs/rbac.md and
// docs/rbac-capabilities.md.
type Role struct {
	Name        string `json:"name"`
	Description string `json:"description,omitempty"`

	// AllowLabels/DenyLabels bind each key to the values that satisfy it, so
	// {"env": {"prod", "stg"}} reads as "env is prod OR stg". The server has
	// always stored and enforced this shape; these were map[string]string
	// until 2.0, which meant a rule with several values per key could not be
	// decoded at all and took the provider and migration tool down with it
	// (#1457). A scalar from the server normalises to a one-element slice.
	AllowLabels map[string][]string `json:"allow-labels,omitempty"`
	AllowNames  []string            `json:"allow-names,omitempty"`
	DenyLabels  map[string][]string `json:"deny-labels,omitempty"`
	DenyNames   []string            `json:"deny-names,omitempty"`

	// AllowAll is an estate-wide grant: the allow side matches EVERY resource
	// on every axis. Deny rules still win over it, so "everything except the
	// sealed ones" stays expressible, and it does NOT raise the role's
	// capabilities — a role granting read still grants read, just everywhere.
	//
	// It exists because label and name rules are exact-match (no wildcards),
	// so covering the estate otherwise meant a shared label on every
	// workspace, which fails in the dangerous direction: a workspace created
	// without the label silently falls outside the role.
	AllowAll bool `json:"allow-all"`

	WorkspacePermission string `json:"workspace-permission"` // read | plan | write | admin
	PoolPermission      string `json:"pool-permission,omitempty"`
	RegistryPermission  string `json:"registry-permission,omitempty"` // read | write | admin (modules + providers)
	CatalogPermission   string `json:"catalog-permission,omitempty"`  // none | read | use | admin

	// Capabilities is the effective capability set (#585). When the role is
	// authored via CreateRoleRequest/UpdateRoleRequest.Capabilities it is the
	// stored truth and the *Permission level fields above become a
	// server-derived summary (a preset name, or the literal "custom" when the
	// caps match no preset). When authored by level, the server still returns
	// the levels' expanded capability set here.
	Capabilities []string `json:"capabilities,omitempty"`

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

	AllowLabels map[string][]string
	AllowNames  []string
	DenyLabels  map[string][]string
	DenyNames   []string

	// AllowAll grants estate-wide. See Role.AllowAll.
	AllowAll bool

	WorkspacePermission string
	PoolPermission      string
	RegistryPermission  string
	CatalogPermission   string

	// Capabilities authors the role's grant directly as explicit
	// "resource:verb" tokens (#585). When set it is the source of truth and
	// the *Permission level fields are ignored (the server derives them as a
	// summary). Leave empty to author by level instead.
	Capabilities []string
}

// UpdateRoleRequest is the partial-update shape. Name is immutable.
// Pointer fields preserve "leave alone" semantics so a vanilla rename
// doesn't clear allow/deny sets. Pass a pointer to an empty value to
// explicitly clear.
type UpdateRoleRequest struct {
	Description *string

	// AllowAll grants estate-wide; nil leaves it unchanged. See Role.AllowAll.
	AllowAll    *bool
	AllowLabels *map[string][]string
	AllowNames  *[]string
	DenyLabels  *map[string][]string
	DenyNames   *[]string

	WorkspacePermission string
	PoolPermission      string
	RegistryPermission  string
	CatalogPermission   string

	// Capabilities replaces the role's grant with the given explicit
	// capability set (#585); when non-empty it wins over the level fields.
	Capabilities []string
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
	if req.AllowAll {
		attrs["allow-all"] = true
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
	if len(req.Capabilities) > 0 {
		attrs["capabilities"] = req.Capabilities
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
	if req.AllowAll != nil {
		attrs["allow-all"] = *req.AllowAll
	}
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
	if len(req.Capabilities) > 0 {
		attrs["capabilities"] = req.Capabilities
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

func mapOrEmpty(m map[string][]string) map[string][]string {
	if m == nil {
		return map[string][]string{}
	}
	return m
}

// labelRule decodes a label rule from the server, which stores JSONB and may
// give a key either a single value or a list of them. Both normalise to a
// slice here so callers have one shape to handle rather than two.
//
// This is the whole of the #1457 fix on the read side: the previous
// map[string]string failed to decode a list-valued rule outright, and because
// listing roles decodes them all, one such role made every role unreadable —
// taking the Terraform provider and the migration tool with it.
type labelRule map[string][]string

func (l *labelRule) UnmarshalJSON(b []byte) error {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(b, &raw); err != nil {
		return err
	}
	out := make(labelRule, len(raw))
	for key, v := range raw {
		var one string
		if err := json.Unmarshal(v, &one); err == nil {
			out[key] = []string{one}
			continue
		}
		var many []string
		if err := json.Unmarshal(v, &many); err != nil {
			return fmt.Errorf("label %q: want a string or a list of strings", key)
		}
		out[key] = many
	}
	*l = out
	return nil
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
		Description         string    `json:"description"`
		AllowAll            bool      `json:"allow-all"`
		AllowLabels         labelRule `json:"allow-labels"`
		AllowNames          []string  `json:"allow-names"`
		DenyLabels          labelRule `json:"deny-labels"`
		DenyNames           []string  `json:"deny-names"`
		WorkspacePermission string    `json:"workspace-permission"`
		PoolPermission      string    `json:"pool-permission"`
		RegistryPermission  string    `json:"registry-permission"`
		CatalogPermission   string    `json:"catalog-permission"`
		Capabilities        []string  `json:"capabilities"`
		BuiltIn             bool      `json:"built-in"`
		CreatedAt           string    `json:"created-at"`
		UpdatedAt           string    `json:"updated-at"`
	}
	if len(item.Attributes) > 0 {
		if err := json.Unmarshal(item.Attributes, &attrs); err != nil {
			return nil, fmt.Errorf("parse role attributes: %w", err)
		}
	}
	return &Role{
		AllowAll:            attrs.AllowAll,
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
		Capabilities:        attrs.Capabilities,
		BuiltIn:             attrs.BuiltIn,
		CreatedAt:           attrs.CreatedAt,
		UpdatedAt:           attrs.UpdatedAt,
	}, nil
}

// ── Role reach preview (#1456) ─────────────────────────────────────────

// Role-reach verdicts, as returned per workspace.
const (
	RoleReachAllowed = "allowed"
	RoleReachDenied  = "denied"
	RoleReachNone    = "none"
)

// Per-workspace notes on a reach result. Each names a path to access that
// exists INDEPENDENTLY of the role being previewed, so a reader is not misled
// into thinking the role is the only thing granting there.
const (
	// RoleReachNoteCatalogClamped: the workspace is catalog-managed, so every
	// non-platform-admin grant is capped at the read floor — a role granting
	// write here does not actually give write.
	RoleReachNoteCatalogClamped = "catalog-clamped"
	// RoleReachNoteEveryoneFloor: labelled access=everyone, so it is readable
	// regardless of this role.
	RoleReachNoteEveryoneFloor = "everyone-floor"
	// RoleReachNoteHasOwner: it has an owner, and an owner holds admin
	// regardless of this role.
	RoleReachNoteHasOwner = "has-owner"
)

// RoleReachWorkspace is one workspace in a reach result, with the reason the
// role's rules landed where they did (e.g. "allow-label:env=prod",
// "deny-name") and the capabilities the role resolves to there.
type RoleReachWorkspace struct {
	ID string `json:"id"`
	// Kind names the resource type ("workspaces", "agent-pools",
	// "registry-modules", "registry-providers", "catalog-items"). Empty on the
	// promoted workspace lists, where it is implied.
	Kind         string            `json:"kind,omitempty"`
	Name         string            `json:"name"`
	Labels       map[string]string `json:"labels,omitempty"`
	OwnerEmail   string            `json:"owner-email,omitempty"`
	Verdict      string            `json:"verdict"`
	Reason       string            `json:"reason,omitempty"`
	Capabilities []string          `json:"capabilities,omitempty"`
	Notes        []string          `json:"notes,omitempty"`
}

// RoleReach is which workspaces a role grants on.
//
// The counts are aggregates over the whole fleet, not over the returned page —
// an operator needs "this rule reaches 4,200 workspaces" to be true rather than
// truncated to whatever fitted. Workspaces holds one page of the granted set;
// Denied holds a bounded sample of what a deny rule removed, which is what
// makes a deny rule safe to write.
type RoleReach struct {
	// Totals across EVERY axis the role's rules govern (workspaces + pools +
	// registry + catalog), not workspaces alone. These are NOT the denominator
	// of the Workspaces/Denied lists below, which are the workspace axis only —
	// read Axes["workspace"].GrantedCount for the workspace count.
	GrantedCount int `json:"granted-count"`
	DeniedCount  int `json:"denied-count"`
	MatchedCount int `json:"matched-count"`

	// Axes is the breakdown, keyed "workspace" | "pool" | "registry" |
	// "catalog". A role's allow/deny rules are matched the same way whatever
	// they are matched against, so one rule reaches agent pools, registry
	// modules and providers, and catalog items as readily as workspaces --
	// reading only the workspace numbers answers a quarter of the question.
	// Capabilities in each block are sliced to that axis.
	Axes map[string]RoleReachAxis `json:"axes,omitempty"`

	// Workspaces/Denied promote the workspace axis to the top level, since it
	// is what most callers want. Equivalent to Axes["workspace"].
	Workspaces      []RoleReachWorkspace `json:"workspaces"`
	Denied          []RoleReachWorkspace `json:"denied,omitempty"`
	DeniedTruncated bool                 `json:"denied-truncated"`
}

// RoleReachAxis is what a role reaches on one capability axis.
type RoleReachAxis struct {
	GrantedCount    int                  `json:"granted-count"`
	DeniedCount     int                  `json:"denied-count"`
	MatchedCount    int                  `json:"matched-count"`
	Resources       []RoleReachWorkspace `json:"resources"`
	Denied          []RoleReachWorkspace `json:"denied,omitempty"`
	DeniedTruncated bool                 `json:"denied-truncated"`
}

// RoleReachOptions pages the granted set. Zero values mean the server default
// (25 per page, first page); PageSize is capped server-side at 100.
type RoleReachOptions struct {
	PageSize   int
	PageNumber int
}

func (o *RoleReachOptions) query() string {
	if o == nil {
		return ""
	}
	q := url.Values{}
	if o.PageSize > 0 {
		q.Set("page[size]", fmt.Sprintf("%d", o.PageSize))
	}
	if o.PageNumber > 0 {
		q.Set("page[number]", fmt.Sprintf("%d", o.PageNumber))
	}
	if len(q) == 0 {
		return ""
	}
	return "?" + q.Encode()
}

type roleReachEnvelope struct {
	Data struct {
		Attributes RoleReach `json:"attributes"`
	} `json:"data"`
}

func parseRoleReach(body []byte) (*RoleReach, error) {
	var doc roleReachEnvelope
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, fmt.Errorf("parse role reach response: %w", err)
	}
	return &doc.Data.Attributes, nil
}

// PreviewRoleReach returns which workspaces a SAVED role currently grants on.
//
// Built-in roles are rejected by the server (422): admin and audit grant
// through the platform path on every workspace, so a label-reach figure for
// them would be true and deeply misleading.
func (c *Client) PreviewRoleReach(ctx context.Context, name string, opts *RoleReachOptions) (*RoleReach, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/roles/"+url.PathEscape(name)+"/preview"+opts.query())
	if err != nil {
		return nil, err
	}
	return parseRoleReach(data)
}

// PreviewUnsavedRoleReach returns which workspaces a role WOULD grant on, for a
// role that does not exist yet. Nothing is persisted.
//
// This is the authoring case: the allow/deny interaction is where rules go
// wrong, and seeing the match before saving is what makes a deny rule safe to
// write. The body goes through the same validation a create does, so a preview
// cannot succeed for a role that could not be saved.
func (c *Client) PreviewUnsavedRoleReach(ctx context.Context, req CreateRoleRequest, opts *RoleReachOptions) (*RoleReach, error) {
	attrs := roleCreateAttrs(req)
	attrs["name"] = req.Name
	body, err := json.Marshal(map[string]any{
		"data": map[string]any{"type": "roles", "attributes": attrs},
	})
	if err != nil {
		return nil, err
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/roles/preview"+opts.query(), body)
	if err != nil {
		return nil, err
	}
	return parseRoleReach(data)
}

// ── The reverse view: who can reach a resource (#1456) ────────────────

// ResourceAccessRole is one role's grant on a resource, with the rule
// responsible and the identities holding the role.
type ResourceAccessRole struct {
	Role         string   `json:"role"`
	Verdict      string   `json:"verdict"`
	Reason       string   `json:"reason,omitempty"`
	Capabilities []string `json:"capabilities,omitempty"`
	Notes        []string `json:"notes,omitempty"`
	HeldBy       []string `json:"held-by,omitempty"`
}

// ResourceAccess answers "who can reach this?" for one resource.
//
// Read PlatformPaths before treating Roles as the whole answer. A list of roles
// reads as complete when it is not: a platform admin reaches everything, an
// owner holds admin on their own resource, and an `access: everyone` label
// makes a thing readable with no role involved at all.
type ResourceAccess struct {
	Resource struct {
		ID         string            `json:"id"`
		Kind       string            `json:"kind"`
		Name       string            `json:"name"`
		Labels     map[string]string `json:"labels,omitempty"`
		OwnerEmail string            `json:"owner-email,omitempty"`
	} `json:"resource"`
	Axis          string               `json:"axis"`
	Roles         []ResourceAccessRole `json:"roles"`
	DeniedRoles   []ResourceAccessRole `json:"denied-roles,omitempty"`
	RoleCount     int                  `json:"role-count"`
	PlatformPaths []string             `json:"platform-paths,omitempty"`
}

type resourceAccessEnvelope struct {
	Data struct {
		Attributes ResourceAccess `json:"attributes"`
	} `json:"data"`
}

// GetResourceAccess reports which roles reach one resource, at what capability,
// and who holds them — the inverse of PreviewRoleReach.
//
// kind is the URL segment for the resource type: "workspaces", "agent-pools",
// "registry-modules", "registry-providers" or "catalog-items". Roles are few,
// so this is unpaged by design.
func (c *Client) GetResourceAccess(ctx context.Context, kind, id string) (*ResourceAccess, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/"+url.PathEscape(kind)+"/"+url.PathEscape(id)+"/access")
	if err != nil {
		return nil, err
	}
	var doc resourceAccessEnvelope
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, fmt.Errorf("parse resource access response: %w", err)
	}
	return &doc.Data.Attributes, nil
}
