package terrapod

import (
	"context"
	"fmt"
	"net/url"
	"strconv"
)

// Ansible inventory: the eight structures a workspace declares (#1968) and the
// resolution they feed (#1967).
//
// # Eight structures, because that is what an ansible inventory is
//
// A host, a group, a host's membership of a group, a group's nesting inside
// another, and a variable attached to a host, to a group or to `all`. The two
// memberships are many-to-many and each is its own addressable row, so a
// second concern can put a host in a group it does not own, and a variable has
// exactly one writer -- which is the whole reason variables are rows here
// rather than a map on their parent.
//
// There is no inventory object to create: there is one inventory per workspace,
// and `InventorySettings` is the 1:1 row carrying its VCS binding. A workspace
// with no settings row and no declared rows has no ansible inventory, which is
// the normal state of a terraform/tofu-only workspace.
//
// # Paths use the canonical /api/v1 prefix with no legacy fallback
//
// Other resources here address /api/terrapod/v1 because they predate the
// canonical native prefix and a lagging server may serve only the alias. These
// endpoints do not exist on any release, so a server old enough to want the
// alias would answer 404 under either spelling -- a fallback would be code that
// can never succeed.
//
// # A variable's value is a string, and a richer shape says so
//
// `Value` is a string and `Structured` says whether to read it as a literal
// source expression rather than as text -- the same arrangement a structured
// workspace variable has. So a list or a number is expressed by setting
// Structured and sending its source, not by typing this field as `any`: a
// Terraform map is homogeneous, and the provider is this SDK's writer.
//
// # A sensitive value never comes back
//
// `Sensitive` is a display flag: the server masks the value in every response,
// so reading a sensitive variable yields the mask and not the secret. It says
// nothing about how the value is STORED.
//
// Storage is a separate, deployment-wide decision. The column is registered for
// app-layer encryption, so where an operator has enabled it every value is
// enveloped whatever `Sensitive` says -- a column cannot be conditionally
// encrypted -- and where they have not, which is the default, the column is
// plaintext and the protection is the datastore's own at-rest encryption. The
// same position as a workspace variable's value.

// MaskedValue is what the server returns in place of a sensitive variable's
// value. Compare against it rather than against a literal: a round-trip that
// writes back what it read would otherwise store the mask as the secret.
const MaskedValue = "***"

// InventorySettings is a workspace's inventory configuration: whether its
// declared rows contribute, and the VCS source merged with them.
//
// Identified by the workspace id, because that is the key -- one inventory per
// workspace, so there is no surrogate id to carry.
//
// The binding is NOT the workspace's terraform binding. Even in one repository
// the root directories differ, and a configure-only workspace has no terraform
// binding at all. A binding points at a DIRECTORY, which ansible reads as one
// source in lexical filename order, so one binding already carries arbitrarily
// many files and ordering within it is the operator's business via filenames.
type InventorySettings struct {
	ID          string `json:"id"`
	WorkspaceID string `json:"workspace-id,omitempty"`
	// IncludePlatform is whether the rows declared through this API contribute
	// to the resolution. False resolves the VCS source alone.
	IncludePlatform bool `json:"include-platform"`
	// VCSConnectionID is empty when no VCS source is bound.
	VCSConnectionID  string   `json:"vcs-connection-id,omitempty"`
	RepoURL          string   `json:"repo-url,omitempty"`
	Branch           string   `json:"branch,omitempty"`
	WorkingDirectory string   `json:"working-directory,omitempty"`
	IgnorePaths      []string `json:"ignore-paths,omitempty"`
	CreatedAt        string   `json:"created-at,omitempty"`
	UpdatedAt        string   `json:"updated-at,omitempty"`
}

// InventoryHost is one host. `Name` is ansible's inventory_hostname, and it is
// the only field a host has -- `ansible_host` is a variable like any other,
// because two homes for one value is one home too many.
type InventoryHost struct {
	ID          string `json:"id"`
	WorkspaceID string `json:"workspace-id,omitempty"`
	// Name is refused if it contains --limit's own operators and separators
	// (`, : ! & ~` or whitespace): a host named with one of those cannot be
	// targeted, and a leading `!` would silently exclude the host it names.
	Name string `json:"name"`
	// GroupCount and VariableCount are counts, not the rows. A host list shows
	// "3 groups, 2 variables"; embedding either would make one request grow
	// with the whole inventory, and the rows are one call away.
	GroupCount    int    `json:"group-count"`
	VariableCount int    `json:"variable-count"`
	CreatedAt     string `json:"created-at,omitempty"`
	UpdatedAt     string `json:"updated-at,omitempty"`
}

// InventoryGroup is one group.
//
// `all` and `ungrouped` are refused as declared names. `all` would collide with
// the rendered document's own root key, and `ungrouped` is derived from the
// membership rows -- both are ansible's to produce.
type InventoryGroup struct {
	ID            string `json:"id"`
	WorkspaceID   string `json:"workspace-id,omitempty"`
	Name          string `json:"name"`
	MemberCount   int    `json:"member-count"`
	ChildCount    int    `json:"child-count"`
	VariableCount int    `json:"variable-count"`
	CreatedAt     string `json:"created-at,omitempty"`
	UpdatedAt     string `json:"updated-at,omitempty"`
}

// InventoryHostGroup is one host's membership of one group -- a `[groupname]`
// host line. Many-to-many, and its own row so that a second concern can create
// it without owning either side.
//
// Immutable: there is nothing to change about it that is not a different row.
type InventoryHostGroup struct {
	ID          string `json:"id"`
	WorkspaceID string `json:"workspace-id,omitempty"`
	HostID      string `json:"host-id,omitempty"`
	GroupID     string `json:"group-id,omitempty"`
	CreatedAt   string `json:"created-at,omitempty"`
}

// InventoryGroupChild is one group nested inside another -- a
// `[groupname:children]` entry. A group may have several parents, and a cycle
// is refused.
//
// Immutable, for the same reason as a membership.
type InventoryGroupChild struct {
	ID            string `json:"id"`
	WorkspaceID   string `json:"workspace-id,omitempty"`
	ParentGroupID string `json:"parent-group-id,omitempty"`
	ChildGroupID  string `json:"child-group-id,omitempty"`
	CreatedAt     string `json:"created-at,omitempty"`
}

// InventoryVar is one variable row, in any of its three kinds.
//
// One type rather than three, because the three are the same four fields with
// different parents: `host_vars/<host>`, `group_vars/<group>` and
// `group_vars/all`. Exactly one parent field is set, and which one says which
// kind this is -- as does the id's prefix (`invhvar-`, `invgvar-`, `invvar-`).
// The methods are separate because the routes are, and because a caller always
// knows which kind it is creating.
type InventoryVar struct {
	ID          string `json:"id"`
	WorkspaceID string `json:"workspace-id,omitempty"`
	// HostID is set only on a host variable, GroupID only on a group variable.
	// Both empty is a variable under `all`.
	HostID  string `json:"host-id,omitempty"`
	GroupID string `json:"group-id,omitempty"`
	Key     string `json:"key"`
	// Value is MaskedValue when Sensitive is true -- the server never returns
	// the stored value.
	Value      string `json:"value"`
	Structured bool   `json:"structured"`
	Sensitive  bool   `json:"sensitive"`
	CreatedAt  string `json:"created-at,omitempty"`
	UpdatedAt  string `json:"updated-at,omitempty"`
}

// ResolvedInventory is what a workspace's inventory resolves to.
//
// Resolved to answer the read, every time. There is no timestamp and nothing
// describing freshness: dynamic inventory was declined (#1970), so every source
// is static and there is no staleness for a caller to reason about. A cache
// exists on the server but is keyed on the content it resolved, so it is
// invisible -- there is nothing to invalidate and nothing to tune.
//
// ANSIBLE produces this, not Terrapod: the server renders its declared rows to
// one YAML document and runs `ansible-inventory --list` over that and the VCS
// directory. The precedence rules, the group DAG, the derivation of `all` and
// `ungrouped`, and --limit expansion are ansible's.
type ResolvedInventory struct {
	ID          string `json:"id"`
	WorkspaceID string `json:"workspace-id,omitempty"`
	HostCount   int    `json:"host-count"`
	GroupCount  int    `json:"group-count"`

	// Hosts is exhaustive -- every host, including one with no variables.
	// Deliberately unlike ansible's own `_meta.hostvars`, where a var-less host
	// is omitted entirely: enumerating a host set from that shape loses hosts
	// silently, so the server builds this from the membership lists with
	// `_meta` merged in.
	Hosts map[string]map[string]any `json:"hosts,omitempty"`
	// Groups is DIRECT membership per group. Ansible does not flatten nesting
	// into a group's host list, so a parent whose members all arrive through a
	// child reports none of its own. Do not read an empty list as "targets
	// nothing" -- read GroupChildren, or ask with a Limit.
	Groups map[string][]string `json:"groups,omitempty"`
	// GroupChildren is the nesting Groups does not carry: parent name to child
	// group names.
	GroupChildren map[string][]string `json:"group-children,omitempty"`
	// Limit is echoed back when the resolution was asked with one.
	Limit string `json:"limit,omitempty"`
}

// PutInventorySettingsRequest is the complete intended state of a workspace's
// inventory settings.
//
// A full replace, not a patch: an absent field takes its default rather than
// keeping the stored value. That is the right shape for Terraform, which always
// knows the whole intended state. A caller changing one field wants
// UpdateInventorySettingsRequest.
type PutInventorySettingsRequest struct {
	IncludePlatform  bool
	VCSConnectionID  string
	RepoURL          string
	Branch           string
	WorkingDirectory string
	IgnorePaths      []string
}

// UpdateInventorySettingsRequest changes some of the settings and leaves the
// rest alone.
//
// Pointers distinguish "leave alone" from "set or clear". ClearVCSConnection
// is a separate flag rather than an empty VCSConnectionID, because a
// relationship is removed by sending it explicitly as null and omitting it
// means something different -- the whole distinction a patch exists to draw.
type UpdateInventorySettingsRequest struct {
	IncludePlatform    *bool
	VCSConnectionID    *string
	ClearVCSConnection bool
	RepoURL            *string
	Branch             *string
	WorkingDirectory   *string
	IgnorePaths        *[]string
}

// CreateInventoryVarRequest declares one variable.
type CreateInventoryVarRequest struct {
	Key string
	// Value is text unless Structured, in which case it is literal source for
	// a list, number, bool or object.
	Value      string
	Structured bool
	Sensitive  bool
}

// UpdateInventoryVarRequest changes one variable. An absent field is left
// alone, which is what lets a caller set Sensitive without resending a value
// it may have read back masked.
//
// Key is renameable. It need not have been -- a rename could have been a delete
// and a create -- but the row carries three other fields and making a caller
// rebuild them to correct a typo is work with nothing behind it. A rename can
// collide, which is the one new way this write answers 409.
type UpdateInventoryVarRequest struct {
	Key        *string
	Value      *string
	Structured *bool
	Sensitive  *bool
}

// ── Settings ─────────────────────────────────────────────────────────────────

// GetInventorySettings reads a workspace's inventory settings.
//
// A NotFoundError means the workspace has no settings row, which is the normal
// default state and reads as "no VCS source bound" -- not as an error. Treat it
// with IsNotFound rather than propagating it.
func (c *Client) GetInventorySettings(
	ctx context.Context, workspaceID string,
) (*InventorySettings, error) {
	data, err := c.Get(ctx, inventoryWorkspacePath(workspaceID, "settings"))
	if err != nil {
		return nil, err
	}
	return parseInventorySettings(data)
}

// PutInventorySettings creates or replaces a workspace's inventory settings.
func (c *Client) PutInventorySettings(
	ctx context.Context, workspaceID string, req PutInventorySettingsRequest,
) (*InventorySettings, error) {
	attrs := map[string]any{
		"include-platform":  req.IncludePlatform,
		"repo-url":          req.RepoURL,
		"branch":            req.Branch,
		"working-directory": req.WorkingDirectory,
	}
	if req.IgnorePaths != nil {
		attrs["ignore-paths"] = req.IgnorePaths
	} else {
		attrs["ignore-paths"] = []string{}
	}
	rels := map[string]any{"vcs-connection": relOrNull("vcs-connections", req.VCSConnectionID)}
	body, err := MarshalResource("inventory-settings", attrs, rels)
	if err != nil {
		return nil, fmt.Errorf("marshal put inventory-settings: %w", err)
	}
	data, err := c.Put(ctx, inventoryWorkspacePath(workspaceID, "settings"), body)
	if err != nil {
		return nil, err
	}
	return parseInventorySettings(data)
}

// UpdateInventorySettings changes part of a workspace's inventory settings.
//
// A NotFoundError means there is nothing to patch; PutInventorySettings creates
// the row.
func (c *Client) UpdateInventorySettings(
	ctx context.Context, workspaceID string, req UpdateInventorySettingsRequest,
) (*InventorySettings, error) {
	attrs := map[string]any{}
	if req.IncludePlatform != nil {
		attrs["include-platform"] = *req.IncludePlatform
	}
	if req.RepoURL != nil {
		attrs["repo-url"] = *req.RepoURL
	}
	if req.Branch != nil {
		attrs["branch"] = *req.Branch
	}
	if req.WorkingDirectory != nil {
		attrs["working-directory"] = *req.WorkingDirectory
	}
	if req.IgnorePaths != nil {
		paths := *req.IgnorePaths
		if paths == nil {
			// A pointer to a NIL slice still means "clear", so it has to send
			// `[]`. Dereferencing straight into the map marshals as JSON null,
			// which the server reads as absent -- so the one request that
			// removes every ignore path would silently do nothing.
			paths = []string{}
		}
		attrs["ignore-paths"] = paths
	}

	var rels map[string]any
	switch {
	case req.ClearVCSConnection:
		rels = map[string]any{"vcs-connection": map[string]any{"data": nil}}
	case req.VCSConnectionID != nil:
		rels = map[string]any{
			"vcs-connection": relOrNull("vcs-connections", *req.VCSConnectionID),
		}
	}

	body, err := MarshalResourceWithIDAndRels(
		workspaceID, "inventory-settings", attrs, rels)
	if err != nil {
		return nil, fmt.Errorf("marshal patch inventory-settings: %w", err)
	}
	data, err := c.Patch(ctx, inventoryWorkspacePath(workspaceID, "settings"), body)
	if err != nil {
		return nil, err
	}
	return parseInventorySettings(data)
}

// DeleteInventorySettings removes a workspace's inventory settings, which
// clears its VCS binding. Declared rows are untouched.
func (c *Client) DeleteInventorySettings(ctx context.Context, workspaceID string) error {
	return c.Delete(ctx, inventoryWorkspacePath(workspaceID, "settings"))
}

// ── Hosts ────────────────────────────────────────────────────────────────────

// CreateInventoryHost declares a host.
func (c *Client) CreateInventoryHost(
	ctx context.Context, workspaceID, name string,
) (*InventoryHost, error) {
	body, err := MarshalResource("inventory-hosts", map[string]any{"name": name}, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create inventory-host: %w", err)
	}
	data, err := c.Post(ctx, inventoryWorkspacePath(workspaceID, "hosts"), body)
	if err != nil {
		return nil, err
	}
	return parseInventoryHost(data)
}

// GetInventoryHost reads one host.
func (c *Client) GetInventoryHost(ctx context.Context, id string) (*InventoryHost, error) {
	data, err := c.Get(ctx, "/api/v1/inventory-hosts/"+url.PathEscape(id))
	if err != nil {
		return nil, err
	}
	return parseInventoryHost(data)
}

// ListInventoryHosts reads one page of a workspace's hosts.
func (c *Client) ListInventoryHosts(
	ctx context.Context, workspaceID string,
) ([]InventoryHost, error) {
	data, err := c.Get(ctx, inventoryWorkspacePath(workspaceID, "hosts"))
	if err != nil {
		return nil, err
	}
	return parseInventoryHostList(data)
}

// ListAllInventoryHosts pages through every host in a workspace.
//
// Worth having rather than relying on the absent-paging default: a `for_each`
// over a few hundred instances is the documented shape for declaring hosts, so
// the collection is routinely larger than one page.
func (c *Client) ListAllInventoryHosts(
	ctx context.Context, workspaceID string,
) ([]InventoryHost, error) {
	return pageThrough(ctx, c, inventoryWorkspacePath(workspaceID, "hosts"),
		parseInventoryHostList)
}

// UpdateInventoryHost renames a host. `name` is the only mutable field it has.
func (c *Client) UpdateInventoryHost(
	ctx context.Context, id, name string,
) (*InventoryHost, error) {
	body, err := MarshalResourceWithID(id, "inventory-hosts", map[string]any{"name": name})
	if err != nil {
		return nil, fmt.Errorf("marshal update inventory-host: %w", err)
	}
	data, err := c.Patch(ctx, "/api/v1/inventory-hosts/"+url.PathEscape(id), body)
	if err != nil {
		return nil, err
	}
	return parseInventoryHost(data)
}

// DeleteInventoryHost removes a host, and with it its variables and
// memberships -- the database cascades them.
func (c *Client) DeleteInventoryHost(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/v1/inventory-hosts/"+url.PathEscape(id))
}

// ── Groups ───────────────────────────────────────────────────────────────────

// CreateInventoryGroup declares a group.
func (c *Client) CreateInventoryGroup(
	ctx context.Context, workspaceID, name string,
) (*InventoryGroup, error) {
	body, err := MarshalResource("inventory-groups", map[string]any{"name": name}, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create inventory-group: %w", err)
	}
	data, err := c.Post(ctx, inventoryWorkspacePath(workspaceID, "groups"), body)
	if err != nil {
		return nil, err
	}
	return parseInventoryGroup(data)
}

// GetInventoryGroup reads one group.
func (c *Client) GetInventoryGroup(ctx context.Context, id string) (*InventoryGroup, error) {
	data, err := c.Get(ctx, "/api/v1/inventory-groups/"+url.PathEscape(id))
	if err != nil {
		return nil, err
	}
	return parseInventoryGroup(data)
}

// ListInventoryGroups reads one page of a workspace's groups.
func (c *Client) ListInventoryGroups(
	ctx context.Context, workspaceID string,
) ([]InventoryGroup, error) {
	data, err := c.Get(ctx, inventoryWorkspacePath(workspaceID, "groups"))
	if err != nil {
		return nil, err
	}
	return parseInventoryGroupList(data)
}

// ListAllInventoryGroups pages through every group in a workspace.
func (c *Client) ListAllInventoryGroups(
	ctx context.Context, workspaceID string,
) ([]InventoryGroup, error) {
	return pageThrough(ctx, c, inventoryWorkspacePath(workspaceID, "groups"),
		parseInventoryGroupList)
}

// UpdateInventoryGroup renames a group.
func (c *Client) UpdateInventoryGroup(
	ctx context.Context, id, name string,
) (*InventoryGroup, error) {
	body, err := MarshalResourceWithID(id, "inventory-groups", map[string]any{"name": name})
	if err != nil {
		return nil, fmt.Errorf("marshal update inventory-group: %w", err)
	}
	data, err := c.Patch(ctx, "/api/v1/inventory-groups/"+url.PathEscape(id), body)
	if err != nil {
		return nil, err
	}
	return parseInventoryGroup(data)
}

// DeleteInventoryGroup removes a group, and with it its variables, its
// memberships and its nestings in both directions. The hosts remain.
func (c *Client) DeleteInventoryGroup(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/v1/inventory-groups/"+url.PathEscape(id))
}

// ── Membership ───────────────────────────────────────────────────────────────

// AddHostToInventoryGroup puts a host in a group.
//
// The same row as AddInventoryGroupToHost, created from the group's side.
// Which one to reach for depends on what the caller is iterating: a loop over a
// group's intended members wants this, a loop over a host's groups wants the
// other, and forcing either to invert its loop buys nothing.
func (c *Client) AddHostToInventoryGroup(
	ctx context.Context, groupID, hostID string,
) (*InventoryHostGroup, error) {
	body, err := MarshalResource("inventory-host-groups", nil,
		map[string]any{"host": relOrNull("inventory-hosts", hostID)})
	if err != nil {
		return nil, fmt.Errorf("marshal create inventory-host-group: %w", err)
	}
	data, err := c.Post(ctx,
		"/api/v1/inventory-groups/"+url.PathEscape(groupID)+"/hosts", body)
	if err != nil {
		return nil, err
	}
	return parseInventoryHostGroup(data)
}

// AddInventoryGroupToHost puts a host in a group, from the host's side.
func (c *Client) AddInventoryGroupToHost(
	ctx context.Context, hostID, groupID string,
) (*InventoryHostGroup, error) {
	body, err := MarshalResource("inventory-host-groups", nil,
		map[string]any{"group": relOrNull("inventory-groups", groupID)})
	if err != nil {
		return nil, fmt.Errorf("marshal create inventory-host-group: %w", err)
	}
	data, err := c.Post(ctx,
		"/api/v1/inventory-hosts/"+url.PathEscape(hostID)+"/groups", body)
	if err != nil {
		return nil, err
	}
	return parseInventoryHostGroup(data)
}

// GetInventoryHostGroup reads one membership.
func (c *Client) GetInventoryHostGroup(
	ctx context.Context, id string,
) (*InventoryHostGroup, error) {
	data, err := c.Get(ctx, "/api/v1/inventory-host-groups/"+url.PathEscape(id))
	if err != nil {
		return nil, err
	}
	return parseInventoryHostGroup(data)
}

// ListInventoryGroupMembers reads one page of a group's memberships.
func (c *Client) ListInventoryGroupMembers(
	ctx context.Context, groupID string,
) ([]InventoryHostGroup, error) {
	data, err := c.Get(ctx,
		"/api/v1/inventory-groups/"+url.PathEscape(groupID)+"/hosts")
	if err != nil {
		return nil, err
	}
	return parseInventoryHostGroupList(data)
}

// ListAllInventoryGroupMembers pages through every membership of a group.
func (c *Client) ListAllInventoryGroupMembers(
	ctx context.Context, groupID string,
) ([]InventoryHostGroup, error) {
	return pageThrough(ctx, c,
		"/api/v1/inventory-groups/"+url.PathEscape(groupID)+"/hosts",
		parseInventoryHostGroupList)
}

// ListInventoryHostMemberships reads one page of a host's memberships.
func (c *Client) ListInventoryHostMemberships(
	ctx context.Context, hostID string,
) ([]InventoryHostGroup, error) {
	data, err := c.Get(ctx, "/api/v1/inventory-hosts/"+url.PathEscape(hostID)+"/groups")
	if err != nil {
		return nil, err
	}
	return parseInventoryHostGroupList(data)
}

// DeleteInventoryHostGroup removes one membership. The host and the group
// remain.
func (c *Client) DeleteInventoryHostGroup(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/v1/inventory-host-groups/"+url.PathEscape(id))
}

// ── Nesting ──────────────────────────────────────────────────────────────────

// AddInventoryGroupChild nests a group inside another -- a
// `[groupname:children]` entry.
func (c *Client) AddInventoryGroupChild(
	ctx context.Context, parentGroupID, childGroupID string,
) (*InventoryGroupChild, error) {
	body, err := MarshalResource("inventory-group-children", nil,
		map[string]any{"child-group": relOrNull("inventory-groups", childGroupID)})
	if err != nil {
		return nil, fmt.Errorf("marshal create inventory-group-child: %w", err)
	}
	data, err := c.Post(ctx,
		"/api/v1/inventory-groups/"+url.PathEscape(parentGroupID)+"/children", body)
	if err != nil {
		return nil, err
	}
	return parseInventoryGroupChild(data)
}

// AddInventoryGroupParent nests a group inside another, from the child's side.
//
// A group may have several parents, so "add a parent to this group" is as
// natural a thing to say as "add a child to that one", and the row is the same
// either way.
func (c *Client) AddInventoryGroupParent(
	ctx context.Context, childGroupID, parentGroupID string,
) (*InventoryGroupChild, error) {
	body, err := MarshalResource("inventory-group-children", nil,
		map[string]any{"parent-group": relOrNull("inventory-groups", parentGroupID)})
	if err != nil {
		return nil, fmt.Errorf("marshal create inventory-group-child: %w", err)
	}
	data, err := c.Post(ctx,
		"/api/v1/inventory-groups/"+url.PathEscape(childGroupID)+"/parents", body)
	if err != nil {
		return nil, err
	}
	return parseInventoryGroupChild(data)
}

// GetInventoryGroupChild reads one nesting.
func (c *Client) GetInventoryGroupChild(
	ctx context.Context, id string,
) (*InventoryGroupChild, error) {
	data, err := c.Get(ctx, "/api/v1/inventory-group-children/"+url.PathEscape(id))
	if err != nil {
		return nil, err
	}
	return parseInventoryGroupChild(data)
}

// ListInventoryGroupChildren reads one page of a group's child nestings.
func (c *Client) ListInventoryGroupChildren(
	ctx context.Context, groupID string,
) ([]InventoryGroupChild, error) {
	data, err := c.Get(ctx,
		"/api/v1/inventory-groups/"+url.PathEscape(groupID)+"/children")
	if err != nil {
		return nil, err
	}
	return parseInventoryGroupChildList(data)
}

// ListInventoryGroupParents reads one page of the nestings that place a group
// inside others.
func (c *Client) ListInventoryGroupParents(
	ctx context.Context, groupID string,
) ([]InventoryGroupChild, error) {
	data, err := c.Get(ctx,
		"/api/v1/inventory-groups/"+url.PathEscape(groupID)+"/parents")
	if err != nil {
		return nil, err
	}
	return parseInventoryGroupChildList(data)
}

// DeleteInventoryGroupChild removes one nesting. Both groups remain.
func (c *Client) DeleteInventoryGroupChild(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/v1/inventory-group-children/"+url.PathEscape(id))
}

// ── Variables ────────────────────────────────────────────────────────────────

// CreateInventoryHostVar adds one entry to a host's `host_vars`.
func (c *Client) CreateInventoryHostVar(
	ctx context.Context, hostID string, req CreateInventoryVarRequest,
) (*InventoryVar, error) {
	return c.createInventoryVar(ctx,
		"/api/v1/inventory-hosts/"+url.PathEscape(hostID)+"/vars",
		"inventory-host-vars", req)
}

// GetInventoryHostVar reads one host variable. A sensitive value reads back as
// MaskedValue.
func (c *Client) GetInventoryHostVar(ctx context.Context, id string) (*InventoryVar, error) {
	return c.getInventoryVar(ctx, "/api/v1/inventory-host-vars/"+url.PathEscape(id))
}

// ListInventoryHostVars reads one page of a host's variables.
func (c *Client) ListInventoryHostVars(
	ctx context.Context, hostID string,
) ([]InventoryVar, error) {
	data, err := c.Get(ctx, "/api/v1/inventory-hosts/"+url.PathEscape(hostID)+"/vars")
	if err != nil {
		return nil, err
	}
	return parseInventoryVarList(data)
}

// UpdateInventoryHostVar changes one host variable.
func (c *Client) UpdateInventoryHostVar(
	ctx context.Context, id string, req UpdateInventoryVarRequest,
) (*InventoryVar, error) {
	return c.updateInventoryVar(ctx,
		"/api/v1/inventory-host-vars/"+url.PathEscape(id), id, "inventory-host-vars", req)
}

// DeleteInventoryHostVar removes one host variable.
func (c *Client) DeleteInventoryHostVar(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/v1/inventory-host-vars/"+url.PathEscape(id))
}

// CreateInventoryGroupVar adds one entry to a group's `group_vars`.
func (c *Client) CreateInventoryGroupVar(
	ctx context.Context, groupID string, req CreateInventoryVarRequest,
) (*InventoryVar, error) {
	return c.createInventoryVar(ctx,
		"/api/v1/inventory-groups/"+url.PathEscape(groupID)+"/vars",
		"inventory-group-vars", req)
}

// GetInventoryGroupVar reads one group variable.
func (c *Client) GetInventoryGroupVar(ctx context.Context, id string) (*InventoryVar, error) {
	return c.getInventoryVar(ctx, "/api/v1/inventory-group-vars/"+url.PathEscape(id))
}

// ListInventoryGroupVars reads one page of a group's variables.
func (c *Client) ListInventoryGroupVars(
	ctx context.Context, groupID string,
) ([]InventoryVar, error) {
	data, err := c.Get(ctx, "/api/v1/inventory-groups/"+url.PathEscape(groupID)+"/vars")
	if err != nil {
		return nil, err
	}
	return parseInventoryVarList(data)
}

// UpdateInventoryGroupVar changes one group variable.
func (c *Client) UpdateInventoryGroupVar(
	ctx context.Context, id string, req UpdateInventoryVarRequest,
) (*InventoryVar, error) {
	return c.updateInventoryVar(ctx,
		"/api/v1/inventory-group-vars/"+url.PathEscape(id), id, "inventory-group-vars", req)
}

// DeleteInventoryGroupVar removes one group variable.
func (c *Client) DeleteInventoryGroupVar(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/v1/inventory-group-vars/"+url.PathEscape(id))
}

// CreateInventoryGlobalVar adds one entry to `group_vars/all`.
//
// Parented on the workspace rather than on a group, because `all` cannot BE a
// group: it is ansible's own root, and the rendered document's root key.
func (c *Client) CreateInventoryGlobalVar(
	ctx context.Context, workspaceID string, req CreateInventoryVarRequest,
) (*InventoryVar, error) {
	return c.createInventoryVar(ctx,
		inventoryWorkspacePath(workspaceID, "vars"), "inventory-global-vars", req)
}

// GetInventoryGlobalVar reads one variable under `all`.
func (c *Client) GetInventoryGlobalVar(ctx context.Context, id string) (*InventoryVar, error) {
	return c.getInventoryVar(ctx, "/api/v1/inventory-global-vars/"+url.PathEscape(id))
}

// ListInventoryGlobalVars reads one page of a workspace's variables under `all`.
func (c *Client) ListInventoryGlobalVars(
	ctx context.Context, workspaceID string,
) ([]InventoryVar, error) {
	data, err := c.Get(ctx, inventoryWorkspacePath(workspaceID, "vars"))
	if err != nil {
		return nil, err
	}
	return parseInventoryVarList(data)
}

// UpdateInventoryGlobalVar changes one variable under `all`.
func (c *Client) UpdateInventoryGlobalVar(
	ctx context.Context, id string, req UpdateInventoryVarRequest,
) (*InventoryVar, error) {
	return c.updateInventoryVar(ctx,
		"/api/v1/inventory-global-vars/"+url.PathEscape(id), id, "inventory-global-vars", req)
}

// DeleteInventoryGlobalVar removes one variable under `all`.
func (c *Client) DeleteInventoryGlobalVar(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/v1/inventory-global-vars/"+url.PathEscape(id))
}

// ── Resolution ───────────────────────────────────────────────────────────────

// GetResolvedInventory reads what a workspace's inventory resolves to.
//
// Live, and it writes nothing. An error here is deliberately fatal rather than
// degrading: a partial or empty host set is a configure silently targeting too
// little, and unlike a policy gate there is no later evaluation to catch it.
func (c *Client) GetResolvedInventory(
	ctx context.Context, workspaceID string,
) (*ResolvedInventory, error) {
	data, err := c.Get(ctx, inventoryWorkspacePath(workspaceID, "resolved"))
	if err != nil {
		return nil, err
	}
	return parseResolvedInventory(data)
}

// GetResolvedInventoryWithLimit resolves and applies a `--limit` pattern.
//
// Ansible's own expansion, so it carries every term ansible does -- a group
// name, a host name, a glob, `!exclusions`, `&intersections`, and `~regex`. It
// expands THROUGH nesting, which a group's own host list in ResolvedInventory
// does not, so this is the authoritative answer to "what would this target".
func (c *Client) GetResolvedInventoryWithLimit(
	ctx context.Context, workspaceID, limit string,
) (*ResolvedInventory, error) {
	q := url.Values{}
	q.Set("limit", limit)
	data, err := c.Get(ctx,
		inventoryWorkspacePath(workspaceID, "resolved")+"?"+q.Encode())
	if err != nil {
		return nil, err
	}
	return parseResolvedInventory(data)
}

// ── Internal helpers ─────────────────────────────────────────────────────────

func inventoryWorkspacePath(workspaceID, leaf string) string {
	return fmt.Sprintf("/api/v1/workspaces/%s/inventory/%s", url.PathEscape(workspaceID), leaf)
}

// relOrNull builds a relationship object, or an explicit null for an empty id.
//
// An explicit null is how a relationship is CLEARED, so an empty id must not
// collapse into an omitted field: the server reads absence as "leave alone".
func relOrNull(resourceType, id string) map[string]any {
	if id == "" {
		return map[string]any{"data": nil}
	}
	return map[string]any{"data": map[string]any{"id": id, "type": resourceType}}
}

// pageThrough walks every page of a collection.
//
// Generic over the element, so the eight structures share one loop rather than
// eight copies that can each stop paging in their own way.
func pageThrough[T any](
	ctx context.Context, c *Client, path string, parse func([]byte) ([]T, error),
) ([]T, error) {
	const pageSize = 100
	var all []T
	for page := 1; ; page++ {
		q := url.Values{}
		q.Set("page[number]", strconv.Itoa(page))
		q.Set("page[size]", strconv.Itoa(pageSize))
		// Every caller passes a bare collection path, so the separator is
		// always `?`. A caller that ever passes a query would need to say so.
		data, err := c.Get(ctx, path+"?"+q.Encode())
		if err != nil {
			return nil, err
		}
		items, err := parse(data)
		if err != nil {
			return nil, err
		}
		all = append(all, items...)

		meta, _ := parseListMeta(data)
		if meta.TotalPages > 0 {
			if page >= meta.TotalPages {
				break
			}
		} else if len(items) < pageSize {
			break
		}
	}
	return all, nil
}

func (c *Client) createInventoryVar(
	ctx context.Context, path, resourceType string, req CreateInventoryVarRequest,
) (*InventoryVar, error) {
	attrs := map[string]any{
		"key":        req.Key,
		"value":      req.Value,
		"structured": req.Structured,
		"sensitive":  req.Sensitive,
	}
	body, err := MarshalResource(resourceType, attrs, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create %s: %w", resourceType, err)
	}
	data, err := c.Post(ctx, path, body)
	if err != nil {
		return nil, err
	}
	return parseInventoryVar(data)
}

func (c *Client) getInventoryVar(ctx context.Context, path string) (*InventoryVar, error) {
	data, err := c.Get(ctx, path)
	if err != nil {
		return nil, err
	}
	return parseInventoryVar(data)
}

func (c *Client) updateInventoryVar(
	ctx context.Context, path, id, resourceType string, req UpdateInventoryVarRequest,
) (*InventoryVar, error) {
	attrs := map[string]any{}
	if req.Key != nil {
		attrs["key"] = *req.Key
	}
	if req.Value != nil {
		attrs["value"] = *req.Value
	}
	if req.Structured != nil {
		attrs["structured"] = *req.Structured
	}
	if req.Sensitive != nil {
		attrs["sensitive"] = *req.Sensitive
	}
	body, err := MarshalResourceWithID(id, resourceType, attrs)
	if err != nil {
		return nil, fmt.Errorf("marshal update %s: %w", resourceType, err)
	}
	data, err := c.Patch(ctx, path, body)
	if err != nil {
		return nil, err
	}
	return parseInventoryVar(data)
}

func parseInventorySettings(body []byte) (*InventorySettings, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse inventory-settings response: %w", err)
	}
	out := &InventorySettings{
		ID:               res.ID,
		WorkspaceID:      GetRelationshipID(res, "workspace"),
		IncludePlatform:  GetBoolAttr(res, "include-platform"),
		VCSConnectionID:  GetRelationshipID(res, "vcs-connection"),
		RepoURL:          GetStringAttr(res, "repo-url"),
		Branch:           GetStringAttr(res, "branch"),
		WorkingDirectory: GetStringAttr(res, "working-directory"),
		IgnorePaths:      GetListAttr(res, "ignore-paths"),
		CreatedAt:        GetStringAttr(res, "created-at"),
		UpdatedAt:        GetStringAttr(res, "updated-at"),
	}
	return out, nil
}

func parseInventoryHost(body []byte) (*InventoryHost, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse inventory-host response: %w", err)
	}
	return inventoryHostFromResource(res), nil
}

func parseInventoryHostList(body []byte) ([]InventoryHost, error) {
	resources, err := ParseResourceList(body)
	if err != nil {
		return nil, fmt.Errorf("parse inventory-host list: %w", err)
	}
	out := make([]InventoryHost, 0, len(resources))
	for i := range resources {
		out = append(out, *inventoryHostFromResource(&resources[i]))
	}
	return out, nil
}

func inventoryHostFromResource(res *Resource) *InventoryHost {
	return &InventoryHost{
		ID:            res.ID,
		WorkspaceID:   GetRelationshipID(res, "workspace"),
		Name:          GetStringAttr(res, "name"),
		GroupCount:    int(GetIntAttr(res, "group-count")),
		VariableCount: int(GetIntAttr(res, "variable-count")),
		CreatedAt:     GetStringAttr(res, "created-at"),
		UpdatedAt:     GetStringAttr(res, "updated-at"),
	}
}

func parseInventoryGroup(body []byte) (*InventoryGroup, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse inventory-group response: %w", err)
	}
	return inventoryGroupFromResource(res), nil
}

func parseInventoryGroupList(body []byte) ([]InventoryGroup, error) {
	resources, err := ParseResourceList(body)
	if err != nil {
		return nil, fmt.Errorf("parse inventory-group list: %w", err)
	}
	out := make([]InventoryGroup, 0, len(resources))
	for i := range resources {
		out = append(out, *inventoryGroupFromResource(&resources[i]))
	}
	return out, nil
}

func inventoryGroupFromResource(res *Resource) *InventoryGroup {
	return &InventoryGroup{
		ID:            res.ID,
		WorkspaceID:   GetRelationshipID(res, "workspace"),
		Name:          GetStringAttr(res, "name"),
		MemberCount:   int(GetIntAttr(res, "member-count")),
		ChildCount:    int(GetIntAttr(res, "child-count")),
		VariableCount: int(GetIntAttr(res, "variable-count")),
		CreatedAt:     GetStringAttr(res, "created-at"),
		UpdatedAt:     GetStringAttr(res, "updated-at"),
	}
}

func parseInventoryHostGroup(body []byte) (*InventoryHostGroup, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse inventory-host-group response: %w", err)
	}
	return inventoryHostGroupFromResource(res), nil
}

func parseInventoryHostGroupList(body []byte) ([]InventoryHostGroup, error) {
	resources, err := ParseResourceList(body)
	if err != nil {
		return nil, fmt.Errorf("parse inventory-host-group list: %w", err)
	}
	out := make([]InventoryHostGroup, 0, len(resources))
	for i := range resources {
		out = append(out, *inventoryHostGroupFromResource(&resources[i]))
	}
	return out, nil
}

func inventoryHostGroupFromResource(res *Resource) *InventoryHostGroup {
	return &InventoryHostGroup{
		ID:          res.ID,
		WorkspaceID: GetRelationshipID(res, "workspace"),
		HostID:      GetRelationshipID(res, "host"),
		GroupID:     GetRelationshipID(res, "group"),
		CreatedAt:   GetStringAttr(res, "created-at"),
	}
}

func parseInventoryGroupChild(body []byte) (*InventoryGroupChild, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse inventory-group-child response: %w", err)
	}
	return inventoryGroupChildFromResource(res), nil
}

func parseInventoryGroupChildList(body []byte) ([]InventoryGroupChild, error) {
	resources, err := ParseResourceList(body)
	if err != nil {
		return nil, fmt.Errorf("parse inventory-group-child list: %w", err)
	}
	out := make([]InventoryGroupChild, 0, len(resources))
	for i := range resources {
		out = append(out, *inventoryGroupChildFromResource(&resources[i]))
	}
	return out, nil
}

func inventoryGroupChildFromResource(res *Resource) *InventoryGroupChild {
	return &InventoryGroupChild{
		ID:            res.ID,
		WorkspaceID:   GetRelationshipID(res, "workspace"),
		ParentGroupID: GetRelationshipID(res, "parent-group"),
		ChildGroupID:  GetRelationshipID(res, "child-group"),
		CreatedAt:     GetStringAttr(res, "created-at"),
	}
}

func parseInventoryVar(body []byte) (*InventoryVar, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse inventory variable response: %w", err)
	}
	return inventoryVarFromResource(res), nil
}

func parseInventoryVarList(body []byte) ([]InventoryVar, error) {
	resources, err := ParseResourceList(body)
	if err != nil {
		return nil, fmt.Errorf("parse inventory variable list: %w", err)
	}
	out := make([]InventoryVar, 0, len(resources))
	for i := range resources {
		out = append(out, *inventoryVarFromResource(&resources[i]))
	}
	return out, nil
}

// inventoryVarFromResource reads a variable of any of the three kinds.
//
// Both parent relationships are read unconditionally, and at most one is
// present: a host variable carries `host`, a group variable `group`, and a
// variable under `all` neither. Branching on the resource type instead would
// make the reader care which kind it has, which is the thing one struct exists
// to avoid.
func inventoryVarFromResource(res *Resource) *InventoryVar {
	return &InventoryVar{
		ID:          res.ID,
		WorkspaceID: GetRelationshipID(res, "workspace"),
		HostID:      GetRelationshipID(res, "host"),
		GroupID:     GetRelationshipID(res, "group"),
		Key:         GetStringAttr(res, "key"),
		Value:       GetStringAttr(res, "value"),
		Structured:  GetBoolAttr(res, "structured"),
		Sensitive:   GetBoolAttr(res, "sensitive"),
		CreatedAt:   GetStringAttr(res, "created-at"),
		UpdatedAt:   GetStringAttr(res, "updated-at"),
	}
}

func parseResolvedInventory(body []byte) (*ResolvedInventory, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse resolved-inventory response: %w", err)
	}
	out := &ResolvedInventory{
		ID:            res.ID,
		WorkspaceID:   GetRelationshipID(res, "workspace"),
		HostCount:     int(GetIntAttr(res, "host-count")),
		GroupCount:    int(GetIntAttr(res, "group-count")),
		Groups:        GetStringListMapAttr(res, "groups"),
		GroupChildren: GetStringListMapAttr(res, "group-children"),
		Limit:         GetStringAttr(res, "limit"),
	}
	if hosts := GetObjectAttr(res, "hosts"); hosts != nil {
		out.Hosts = make(map[string]map[string]any, len(hosts))
		for name, raw := range hosts {
			if vars, ok := raw.(map[string]any); ok {
				out.Hosts[name] = vars
			} else {
				// A host with no variables arrives as an empty object; any
				// other shape is the server disagreeing with its own contract,
				// and an empty map is the honest reading rather than dropping
				// the host from the set entirely.
				out.Hosts[name] = map[string]any{}
			}
		}
	}
	return out, nil
}
