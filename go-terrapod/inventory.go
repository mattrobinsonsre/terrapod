package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"strconv"
)

// Ansible inventory: the hosts a workspace declares (#1968), the inventory
// object they resolve into, and the snapshots a configure targets against
// (#1967).
//
// # Paths use the canonical /api/v1 prefix with no legacy fallback
//
// Other resources here address /api/terrapod/v1 because they predate the
// canonical native prefix and a lagging server may serve only the alias. These
// endpoints do not exist on any release, so a server old enough to want the
// alias would answer 404 under either spelling -- a fallback would be code that
// can never succeed. Calling the canonical path keeps the SDK honest about
// where the surface lives.
//
// # Host variables are strings on this path, and that is a Terraform constraint
//
// The API stores a host variable as arbitrary JSON, because ansible does and
// because a git or UI source may legitimately supply a list or a number. This
// SDK types them as map[string]string, because its writer is the Terraform
// provider and a Terraform map is homogeneous.
//
// So a variable that has to be a list or a number belongs in the playbook
// repository's group_vars/host_vars, which is where an ansible operator keeps
// such a thing anyway. Note the consequence rather than discovering it: reading
// an item whose variables were set to a richer shape by some other writer
// yields a nil map, because GetMapAttr cannot represent it.

// InventoryItem is one host a workspace declares through its own Terraform.
type InventoryItem struct {
	ID          string `json:"id"`
	WorkspaceID string `json:"workspace-id,omitempty"`
	// Name is the inventory hostname. The API refuses names containing
	// --limit's own operators and separators (`, : ! & ~` and whitespace),
	// because a host named with one of those cannot be targeted -- and a
	// leading `!` would silently exclude the host it names.
	Name string `json:"name"`
	// Address populates ansible_host. Optional: a host whose name already
	// resolves needs none. An explicit ansible_host in Vars wins.
	Address string `json:"address,omitempty"`
	// Groups are declared group names. Never `all` or `ungrouped`, which
	// ansible derives and the API refuses as declared names.
	Groups    []string          `json:"groups,omitempty"`
	Vars      map[string]string `json:"vars,omitempty"`
	CreatedAt string            `json:"created-at,omitempty"`
	UpdatedAt string            `json:"updated-at,omitempty"`
}

// Inventory is a workspace's ordered set of inventory sources.
type Inventory struct {
	ID          string `json:"id"`
	WorkspaceID string `json:"workspace-id,omitempty"`
	Name        string `json:"name"`
	Description string `json:"description,omitempty"`
	// APIResolvable is false once a source needs ansible to parse, which only
	// a runner has. A false here is why a snapshot may be older than the
	// declared items.
	APIResolvable bool              `json:"api-resolvable"`
	Sources       []InventorySource `json:"sources,omitempty"`
	CreatedAt     string            `json:"created-at,omitempty"`
	UpdatedAt     string            `json:"updated-at,omitempty"`
}

// InventorySource is one entry in an inventory's `-i` ordering. A lower
// Position resolves first, so a higher one wins a conflicting host variable.
type InventorySource struct {
	ID            string         `json:"id"`
	Position      int            `json:"position"`
	Kind          string         `json:"kind"`
	Config        map[string]any `json:"config,omitempty"`
	APIResolvable bool           `json:"api-resolvable"`
	CreatedAt     string         `json:"created-at,omitempty"`
}

// InventoryVersion is a snapshot of a resolved inventory.
//
// Hosts is exhaustive -- every host, including one with no variables -- which
// is deliberately unlike ansible's own `_meta.hostvars`, where a var-less host
// is omitted entirely. Enumerating a host set from that shape loses hosts
// silently.
type InventoryVersion struct {
	ID          string `json:"id"`
	InventoryID string `json:"inventory-id,omitempty"`
	HostCount   int    `json:"host-count"`
	GroupCount  int    `json:"group-count"`
	// ProducedBy is "api" or "runner".
	ProducedBy    string `json:"produced-by"`
	ProducedByRef string `json:"produced-by-ref,omitempty"`
	// TakenAt is when this resolution came to be -- NOT how stale it is.
	// A read resolves live for an inventory the API can resolve itself, so
	// this moves only when the resolution does. It IS a staleness reading for
	// an inventory carrying a source that needs ansible, because that one is
	// served from the newest snapshot a runner posted.
	TakenAt string `json:"taken-at"`

	// Populated by a read of one snapshot; absent from a list.
	Hosts  map[string]map[string]any `json:"hosts,omitempty"`
	Groups map[string][]string       `json:"groups,omitempty"`
	// AnsibleInventory is the same resolution in the shape
	// `ansible-inventory --list` produces, rendered by the server rather than
	// stored twice.
	AnsibleInventory map[string]any `json:"ansible-inventory,omitempty"`
}

// InventoryLimitPreview is which hosts a --limit pattern would select.
//
// Advisory: the authoritative expansion is taken in the runner at the start of
// a configure. A `~regex` term is refused rather than matched, because an empty
// target set for a pattern ansible would have expanded is worse than no answer.
type InventoryLimitPreview struct {
	Limit       string   `json:"limit"`
	Hosts       []string `json:"hosts"`
	HostCount   int      `json:"host-count"`
	OfHostCount int      `json:"of-host-count"`
	TakenAt     string   `json:"taken-at"`
}

// CreateInventoryItemRequest declares one host.
type CreateInventoryItemRequest struct {
	Name    string
	Address string
	Groups  []string
	Vars    map[string]string
}

// UpdateInventoryItemRequest changes one host.
//
// Pointers distinguish "leave alone" from "set or clear": a nil Groups omits
// the attribute, while a pointer to an empty slice sends `[]` and removes the
// host from every group. Collapsing those would make a cleared list impossible
// to express.
type UpdateInventoryItemRequest struct {
	Name    string
	Address *string
	Groups  *[]string
	Vars    *map[string]string
}

// CreateInventoryRequest creates a named inventory.
type CreateInventoryRequest struct {
	Name        string
	Description string
}

// RecordInventoryVersionRequest posts a resolution a runner performed.
//
// Runner-token only: a snapshot records what a resolve actually found, so it is
// written by the thing that ran it.
type RecordInventoryVersionRequest struct {
	Hosts  map[string]map[string]any
	Groups map[string][]string
}

// ── Declared items ───────────────────────────────────────────────────────────

// CreateInventoryItem declares a host in a workspace's inventory.
func (c *Client) CreateInventoryItem(
	ctx context.Context, workspaceID string, req CreateInventoryItemRequest,
) (*InventoryItem, error) {
	body, err := MarshalResource("inventory-items", inventoryItemCreateAttrs(req), nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create inventory-item: %w", err)
	}
	data, err := c.Post(ctx,
		fmt.Sprintf("/api/v1/workspaces/%s/inventory-items", url.PathEscape(workspaceID)),
		body)
	if err != nil {
		return nil, err
	}
	return parseInventoryItem(data)
}

// GetInventoryItem reads one declared host.
func (c *Client) GetInventoryItem(ctx context.Context, id string) (*InventoryItem, error) {
	data, err := c.Get(ctx, "/api/v1/inventory-items/"+url.PathEscape(id))
	if err != nil {
		return nil, err
	}
	return parseInventoryItem(data)
}

// ListInventoryItems reads one page of a workspace's declared hosts.
func (c *Client) ListInventoryItems(
	ctx context.Context, workspaceID string,
) ([]InventoryItem, error) {
	data, err := c.Get(ctx,
		fmt.Sprintf("/api/v1/workspaces/%s/inventory-items", url.PathEscape(workspaceID)))
	if err != nil {
		return nil, err
	}
	return parseInventoryItemList(data)
}

// ListAllInventoryItems pages through every declared host in a workspace.
//
// Worth having rather than relying on the absent-paging default: a `for_each`
// over a few hundred instances is the documented shape for this resource, so
// the collection is routinely larger than one page.
func (c *Client) ListAllInventoryItems(
	ctx context.Context, workspaceID string,
) ([]InventoryItem, error) {
	const pageSize = 100
	var all []InventoryItem
	for page := 1; ; page++ {
		q := url.Values{}
		q.Set("page[number]", strconv.Itoa(page))
		q.Set("page[size]", strconv.Itoa(pageSize))
		data, err := c.Get(ctx, fmt.Sprintf(
			"/api/v1/workspaces/%s/inventory-items?%s", url.PathEscape(workspaceID), q.Encode()))
		if err != nil {
			return nil, err
		}
		items, err := parseInventoryItemList(data)
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

// UpdateInventoryItem changes one declared host.
func (c *Client) UpdateInventoryItem(
	ctx context.Context, id string, req UpdateInventoryItemRequest,
) (*InventoryItem, error) {
	body, err := MarshalResourceWithID(id, "inventory-items", inventoryItemUpdateAttrs(req))
	if err != nil {
		return nil, fmt.Errorf("marshal update inventory-item: %w", err)
	}
	data, err := c.Patch(ctx, "/api/v1/inventory-items/"+url.PathEscape(id), body)
	if err != nil {
		return nil, err
	}
	return parseInventoryItem(data)
}

// DeleteInventoryItem removes one declared host.
//
// This is what `terraform destroy` does, one resource at a time, because each
// host is its own resource in state. Removing a workspace's hosts empties the
// target set of every configure definition reading that inventory.
func (c *Client) DeleteInventoryItem(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/v1/inventory-items/"+url.PathEscape(id))
}

// ── The inventory object ─────────────────────────────────────────────────────

// CreateInventory creates a named inventory with its `platform` source.
func (c *Client) CreateInventory(
	ctx context.Context, workspaceID string, req CreateInventoryRequest,
) (*Inventory, error) {
	attrs := map[string]any{"name": req.Name}
	if req.Description != "" {
		attrs["description"] = req.Description
	}
	body, err := MarshalResource("inventories", attrs, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create inventory: %w", err)
	}
	data, err := c.Post(ctx,
		fmt.Sprintf("/api/v1/workspaces/%s/inventories", url.PathEscape(workspaceID)), body)
	if err != nil {
		return nil, err
	}
	return parseInventory(data)
}

// GetInventory reads one inventory and its ordered sources.
func (c *Client) GetInventory(ctx context.Context, id string) (*Inventory, error) {
	data, err := c.Get(ctx, "/api/v1/inventories/"+url.PathEscape(id))
	if err != nil {
		return nil, err
	}
	return parseInventory(data)
}

// ListInventories reads a workspace's inventories.
//
// Empty for a workspace that has never declared a host: nothing is created
// until something uses it.
func (c *Client) ListInventories(
	ctx context.Context, workspaceID string,
) ([]Inventory, error) {
	data, err := c.Get(ctx,
		fmt.Sprintf("/api/v1/workspaces/%s/inventories", url.PathEscape(workspaceID)))
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, fmt.Errorf("parse inventory list: %w", err)
	}
	out := make([]Inventory, 0, len(resources))
	for i := range resources {
		out = append(out, *inventoryFromResource(&resources[i]))
	}
	return out, nil
}

// DeleteInventory removes an inventory and its snapshots. Declared items are
// not touched: they belong to the workspace, and an inventory is a view.
func (c *Client) DeleteInventory(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/v1/inventories/"+url.PathEscape(id))
}

// ── Resolution and snapshots ─────────────────────────────────────────────────

// GetResolvedInventory reads what an inventory currently resolves to.
//
// LIVE for an inventory the API can resolve itself -- which is every inventory
// that exists today, because the only declared source kind is platform and
// resolving that is a database query. A row is written only when the
// resolution has actually moved, so reading does not evict the bounded
// snapshot history a configure pins.
//
// Where a source needs ansible the newest snapshot a runner posted is served
// instead, and a ConflictError is returned when there is none -- the API
// refuses rather than resolving what it can, because a partial resolution is
// a target set that is silently too small.
func (c *Client) GetResolvedInventory(
	ctx context.Context, inventoryID string,
) (*InventoryVersion, error) {
	data, err := c.Get(ctx, "/api/v1/inventories/"+url.PathEscape(inventoryID)+"/resolved")
	if err != nil {
		return nil, err
	}
	return parseInventoryVersion(data)
}

// ResolveInventory records a snapshot of what the inventory resolves to now.
//
// It is NOT how a caller gets fresh data: GetResolvedInventory and the limit
// preview are already live for an inventory the API can resolve itself. What
// this guarantees is that the bounded snapshot history HOLDS a row describing
// the current resolution, which is what a configure pins. It takes the same
// stamped path a read does, so it returns the existing row unchanged when the
// resolution has not moved.
func (c *Client) ResolveInventory(
	ctx context.Context, inventoryID string,
) (*InventoryVersion, error) {
	data, err := c.Post(ctx,
		"/api/v1/inventories/"+url.PathEscape(inventoryID)+"/actions/resolve", nil)
	if err != nil {
		return nil, err
	}
	return parseInventoryVersion(data)
}

// ListInventoryVersions reads the snapshot history, newest first. Contents are
// omitted; read one snapshot to get them.
func (c *Client) ListInventoryVersions(
	ctx context.Context, inventoryID string,
) ([]InventoryVersion, error) {
	data, err := c.Get(ctx, "/api/v1/inventories/"+url.PathEscape(inventoryID)+"/versions")
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, fmt.Errorf("parse inventory-version list: %w", err)
	}
	out := make([]InventoryVersion, 0, len(resources))
	for i := range resources {
		out = append(out, *inventoryVersionFromResource(&resources[i]))
	}
	return out, nil
}

// RecordInventoryVersion posts a resolution a runner performed. Runner-token
// only; see RecordInventoryVersionRequest.
func (c *Client) RecordInventoryVersion(
	ctx context.Context, inventoryID string, req RecordInventoryVersionRequest,
) (*InventoryVersion, error) {
	attrs := map[string]any{
		"hosts":  req.Hosts,
		"groups": req.Groups,
	}
	if req.Hosts == nil {
		attrs["hosts"] = map[string]map[string]any{}
	}
	if req.Groups == nil {
		attrs["groups"] = map[string][]string{}
	}
	body, err := MarshalResource("inventory-versions", attrs, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal inventory-version: %w", err)
	}
	data, err := c.Post(ctx,
		"/api/v1/inventories/"+url.PathEscape(inventoryID)+"/versions", body)
	if err != nil {
		return nil, err
	}
	return parseInventoryVersion(data)
}

// PreviewInventoryLimit answers which hosts a --limit pattern would select,
// against the inventory's latest snapshot.
func (c *Client) PreviewInventoryLimit(
	ctx context.Context, inventoryID, limit string,
) (*InventoryLimitPreview, error) {
	body, err := MarshalResource(
		"inventory-limit-previews", map[string]any{"limit": limit}, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal limit preview: %w", err)
	}
	data, err := c.Post(ctx,
		"/api/v1/inventories/"+url.PathEscape(inventoryID)+"/actions/preview-limit", body)
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse limit preview: %w", err)
	}
	return &InventoryLimitPreview{
		Limit:       GetStringAttr(res, "limit"),
		Hosts:       GetListAttr(res, "hosts"),
		HostCount:   int(GetIntAttr(res, "host-count")),
		OfHostCount: int(GetIntAttr(res, "of-host-count")),
		TakenAt:     GetStringAttr(res, "taken-at"),
	}, nil
}

// ── Internal helpers ─────────────────────────────────────────────────────────

func inventoryItemCreateAttrs(req CreateInventoryItemRequest) map[string]any {
	attrs := map[string]any{"name": req.Name}
	if req.Address != "" {
		attrs["address"] = req.Address
	}
	if req.Groups != nil {
		attrs["groups"] = req.Groups
	}
	if req.Vars != nil {
		attrs["vars"] = req.Vars
	}
	return attrs
}

func inventoryItemUpdateAttrs(req UpdateInventoryItemRequest) map[string]any {
	attrs := map[string]any{}
	if req.Name != "" {
		attrs["name"] = req.Name
	}
	if req.Address != nil {
		attrs["address"] = *req.Address
	}
	if req.Groups != nil {
		// A pointer to a NIL slice still means "clear", so it has to send `[]`.
		// Dereferencing straight into the map marshals as JSON null, which the
		// server reads as absent -- so the one request that removes a host from
		// every group would silently do nothing. Same shape as
		// `module_autodiscovery_rules`' ignore-patterns.
		groups := *req.Groups
		if groups == nil {
			groups = []string{}
		}
		attrs["groups"] = groups
	}
	if req.Vars != nil {
		hostVars := *req.Vars
		if hostVars == nil {
			hostVars = map[string]string{}
		}
		attrs["vars"] = hostVars
	}
	return attrs
}

func parseInventoryItem(body []byte) (*InventoryItem, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse inventory-item response: %w", err)
	}
	return inventoryItemFromResource(res), nil
}

func parseInventoryItemList(body []byte) ([]InventoryItem, error) {
	resources, err := ParseResourceList(body)
	if err != nil {
		return nil, fmt.Errorf("parse inventory-item list: %w", err)
	}
	out := make([]InventoryItem, 0, len(resources))
	for i := range resources {
		out = append(out, *inventoryItemFromResource(&resources[i]))
	}
	return out, nil
}

func inventoryItemFromResource(res *Resource) *InventoryItem {
	item := &InventoryItem{
		ID:        res.ID,
		Name:      GetStringAttr(res, "name"),
		Address:   GetStringAttr(res, "address"),
		Groups:    GetListAttr(res, "groups"),
		Vars:      GetMapAttr(res, "vars"),
		CreatedAt: GetStringAttr(res, "created-at"),
		UpdatedAt: GetStringAttr(res, "updated-at"),
	}
	if v := GetRelationshipID(res, "workspace"); v != "" {
		item.WorkspaceID = v
	}
	return item
}

func parseInventory(body []byte) (*Inventory, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse inventory response: %w", err)
	}
	return inventoryFromResource(res), nil
}

func inventoryFromResource(res *Resource) *Inventory {
	out := &Inventory{
		ID:            res.ID,
		Name:          GetStringAttr(res, "name"),
		Description:   GetStringAttr(res, "description"),
		APIResolvable: GetBoolAttr(res, "api-resolvable"),
		CreatedAt:     GetStringAttr(res, "created-at"),
		UpdatedAt:     GetStringAttr(res, "updated-at"),
	}
	if v := GetRelationshipID(res, "workspace"); v != "" {
		out.WorkspaceID = v
	}
	out.Sources = inventorySourcesFromAttr(res)
	return out
}

// inventorySourcesFromAttr decodes attributes.sources.
//
// A source is a plain object inside the inventory's attributes rather than a
// nested resource or a relationship, because it has no route of its own -- see
// the server's _source_json for why. InventorySource's own tags match that
// shape field for field, so this is a straight unmarshal rather than a
// hand-walked map: going through `any` would decode every position as a
// float64, and position is the `-i` ordering that decides which source wins a
// conflicting host variable.
func inventorySourcesFromAttr(res *Resource) []InventorySource {
	raw, ok := res.Attributes["sources"]
	if !ok || len(raw) == 0 || string(raw) == "null" {
		return nil
	}
	var sources []InventorySource
	if err := json.Unmarshal(raw, &sources); err != nil {
		return nil
	}
	return sources
}

func parseInventoryVersion(body []byte) (*InventoryVersion, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse inventory-version response: %w", err)
	}
	return inventoryVersionFromResource(res), nil
}

func inventoryVersionFromResource(res *Resource) *InventoryVersion {
	out := &InventoryVersion{
		ID:            res.ID,
		HostCount:     int(GetIntAttr(res, "host-count")),
		GroupCount:    int(GetIntAttr(res, "group-count")),
		ProducedBy:    GetStringAttr(res, "produced-by"),
		ProducedByRef: GetStringAttr(res, "produced-by-ref"),
		TakenAt:       GetStringAttr(res, "taken-at"),
	}
	if v := GetRelationshipID(res, "inventory"); v != "" {
		out.InventoryID = v
	}
	if hosts := GetObjectAttr(res, "hosts"); hosts != nil {
		out.Hosts = make(map[string]map[string]any, len(hosts))
		for name, raw := range hosts {
			if vars, ok := raw.(map[string]any); ok {
				out.Hosts[name] = vars
			} else {
				// A host with no variables may arrive as an empty object; any
				// other shape is the server disagreeing with its own contract,
				// and an empty map is the honest reading rather than dropping
				// the host from the set entirely.
				out.Hosts[name] = map[string]any{}
			}
		}
	}
	out.Groups = GetAudienceMapAttr(res, "groups")
	out.AnsibleInventory = GetObjectAttr(res, "ansible-inventory")
	return out
}
