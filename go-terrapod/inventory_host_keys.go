package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
)

// Approved SSH host keys: the ninth inventory structure (#2592).
//
// # Why Terrapod models this at all
//
// Ansible has no inventory-level model of an approved host key. Its own
// `known_hosts` module manages a file on a managed node, and host-key checking
// on the CONTROLLER is a boolean plus whatever `known_hosts` file the process
// happens to find. So "this host is trusted to present these keys" has nowhere
// to live in an inventory, and the only two answers available to an operator are
// to turn checking off or to ship a file out of band. Terrapod declares the
// trust set as data and hands off to OpenSSH, which is the component that
// actually decides: the runner renders the rows to a `known_hosts` file.
//
// # A host exposes MANY keys and all of them belong to its trust set
//
// A server offers one key per algorithm it is configured for, and a client
// picks by its own `HostKeyAlgorithms` preference -- so nothing here picks one
// per algorithm family, and a consumer that tried to would refuse the host
// whenever the client's preference did not match the one it kept. A host also
// legitimately holds TWO keys of one type while it is being rotated, which is
// why the row's identity is the host, the algorithm AND the blob rather than
// just the first two.
//
// # A key's identity is immutable; only its marker is not
//
// The algorithm and the blob are two thirds of the row's unique constraint, so
// changing either means a different key: that is a delete and a declare, and the
// server refuses a PATCH that supplies either with 422. UpdateInventoryHostKey
// therefore carries only Marker. Editing them in place would let a caller
// rewrite what a host is trusted to present while keeping the row's id, which is
// the one audit trail a trust anchor should not have.
//
// # Nothing here is secret
//
// A host's public key is public. PublicKey comes back in full -- unlike a
// sensitive InventoryVar, which reads back as MaskedValue -- because masking it
// would hide the one value an operator needs to read at the moment the feature
// exists for, which is a host presenting something else.

// ApprovedHostKey is one `known_hosts` line's worth of trust: the algorithm, the
// key, and the optional marker that qualifies it.
//
// Carried on its own, without the row's identity, because the workspace-wide
// overlay reports exactly these attributes and no id -- see
// InventoryHostKeysByHost. InventoryHostKey embeds it and adds the identity the
// per-host and per-key routes do carry.
type ApprovedHostKey struct {
	// KeyType is the algorithm, spelled as OpenSSH spells it in the second field
	// of a known_hosts line: `ssh-ed25519`, `ecdsa-sha2-nistp256`,
	// `ecdsa-sha2-nistp384`, `ecdsa-sha2-nistp521`, `ssh-rsa`, `rsa-sha2-256`
	// or `rsa-sha2-512`. Validated against that closed set by the server, and
	// the reason is the direction a typo fails in: an unrecognised algorithm in
	// a declared trust anchor silently never matches, and the symptom reads as
	// "the host rotated its key" rather than as a configuration error.
	KeyType string `json:"key-type"`
	// PublicKey is the base64 blob ALONE -- the third field of a known_hosts
	// line, with no algorithm prefix and no host field. Those are the line's
	// other two fields and the server refuses a value that looks like a whole
	// line, because stored as the blob it would render with the host and
	// algorithm doubled and no client would match it.
	//
	// The server also checks the blob's own embedded algorithm name against
	// KeyType. An SSH public key is a length-prefixed algorithm name followed
	// by its parameters, so the blob says what it is; note that an
	// `rsa-sha2-256` or `rsa-sha2-512` key embeds `ssh-rsa`, because those name
	// signature algorithms over an RSA key.
	PublicKey string `json:"public-key"`
	// Marker is the optional first field of a known_hosts line, WITHOUT its
	// `@`: `cert-authority` or `revoked`. Empty is an ordinary entry.
	//
	// Per line rather than per algorithm, which is what makes `revoked` useful:
	// the outgoing key of a rotation stays declared and is refused, so a replay
	// of it is rejected rather than silently treated as an unknown host.
	//
	// Empty and JSON null are the same thing here and the server normalises
	// both to null, so this is a plain string rather than a pointer -- there is
	// no third state for a nil to carry.
	Marker string `json:"marker,omitempty"`

	CreatedAt string `json:"created-at,omitempty"`
	UpdatedAt string `json:"updated-at,omitempty"`
}

// InventoryHostKey is one addressable approved-key row.
//
// There is no WorkspaceID: the server's wire shape carries a `host`
// relationship and nothing else, and the host is enough to reach the workspace.
// A field nothing ever assigns reads as an answer, so it is absent rather than
// permanently empty.
type InventoryHostKey struct {
	ID string `json:"id"`
	// HostID is the host this key is approved for. A key can only exist for a
	// host Terrapod DECLARES -- a host appearing only in a VCS-sourced
	// inventory file has no row to hang one on, exactly as it can hold no
	// Terrapod-declared variable.
	HostID string `json:"host-id,omitempty"`

	ApprovedHostKey
}

// InventoryHostKeysByHost is every approved key for one host, as the
// workspace-wide overlay reports it.
//
// Meant to be read beside GetResolvedInventory: together the two answer "what
// would a configure target, and what is each of those hosts trusted to
// present". Kept separate from the resolved document on purpose -- that one is
// ansible's own answer, normalised, and this is Terrapod's.
//
// Keyed on the host NAME, because that is what the resolved document is keyed
// on. Which also means this route carries no key ids: to address a key, list
// the host's own keys with ListInventoryHostKeys.
type InventoryHostKeysByHost struct {
	// HostName is ansible's inventory_hostname, and the resource id on this
	// route.
	HostName string            `json:"host-name"`
	Keys     []ApprovedHostKey `json:"keys,omitempty"`
}

// CreateInventoryHostKeyRequest declares one approved key for a host.
type CreateInventoryHostKeyRequest struct {
	KeyType   string
	PublicKey string
	// Marker is `cert-authority`, `revoked`, or empty for an ordinary entry.
	Marker string
}

// UpdateInventoryHostKeyRequest changes a key's marker, which is the only
// mutable thing about it.
//
// One field, and no KeyType or PublicKey, because the server refuses either
// with 422: they are the row's identity. A pointer so that clearing the marker
// is expressible -- nil leaves it alone, and a pointer to "" removes it.
type UpdateInventoryHostKeyRequest struct {
	Marker *string
}

// ── Per-host and per-key routes ──────────────────────────────────────────────

// CreateInventoryHostKey declares one approved key for a host.
//
// Refused with 409 if this host already has that exact key under that exact
// algorithm. Two keys of ONE algorithm are allowed -- a rotation needs it --
// but the same key twice is not.
func (c *Client) CreateInventoryHostKey(
	ctx context.Context, hostID string, req CreateInventoryHostKeyRequest,
) (*InventoryHostKey, error) {
	attrs := map[string]any{
		"key-type":   req.KeyType,
		"public-key": req.PublicKey,
	}
	// Omitted rather than sent as "": the server reads both as "no marker", and
	// sending the empty string would put a value on the wire that means the
	// absence of one.
	if req.Marker != "" {
		attrs["marker"] = req.Marker
	}
	body, err := MarshalResource("inventory-host-keys", attrs, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create inventory-host-key: %w", err)
	}
	data, err := c.Post(ctx, inventoryHostKeysPath(hostID), body)
	if err != nil {
		return nil, err
	}
	return parseInventoryHostKey(data)
}

// ListInventoryHostKeys reads a host's approved keys.
//
// Ordered by `(key-type, public-key)` rather than by creation time, so a
// rendered `known_hosts` file is byte-stable: consumers diff that file, and an
// insertion-ordered listing would make an unchanged key set look changed after a
// re-create.
//
// There is no ListAll counterpart, deliberately. A host's key set is capped far
// below one page -- a host offers one key per algorithm it is configured for --
// so a single request always returns the whole set, and a paging loop would be
// surface with nothing behind it.
func (c *Client) ListInventoryHostKeys(
	ctx context.Context, hostID string,
) ([]InventoryHostKey, error) {
	data, err := c.Get(ctx, inventoryHostKeysPath(hostID))
	if err != nil {
		return nil, err
	}
	return parseInventoryHostKeyList(data)
}

// GetInventoryHostKey reads one approved key.
func (c *Client) GetInventoryHostKey(
	ctx context.Context, id string,
) (*InventoryHostKey, error) {
	data, err := c.Get(ctx, inventoryHostKeyPath(id))
	if err != nil {
		return nil, err
	}
	return parseInventoryHostKey(data)
}

// UpdateInventoryHostKey changes a key's marker.
//
// The marker is the only mutable thing about a key: to change the algorithm or
// the blob, delete this entry and declare the new one, so the change is visible
// as what it is.
func (c *Client) UpdateInventoryHostKey(
	ctx context.Context, id string, req UpdateInventoryHostKeyRequest,
) (*InventoryHostKey, error) {
	attrs := map[string]any{}
	if req.Marker != nil {
		// Sent even when empty, which is what CLEARS it. The server
		// distinguishes "supplied as null/empty" from "absent", so an omitted
		// field leaves the marker alone and a present one replaces it.
		attrs["marker"] = *req.Marker
	}
	body, err := MarshalResourceWithID(id, "inventory-host-keys", attrs)
	if err != nil {
		return nil, fmt.Errorf("marshal update inventory-host-key: %w", err)
	}
	data, err := c.Patch(ctx, inventoryHostKeyPath(id), body)
	if err != nil {
		return nil, err
	}
	return parseInventoryHostKey(data)
}

// DeleteInventoryHostKey withdraws one approved key.
//
// The host remains, and so do its other keys. Withdrawing the key a host
// actually presents makes it an unknown host to the next configure, which is the
// point: this is the trust set, not a cache.
func (c *Client) DeleteInventoryHostKey(ctx context.Context, id string) error {
	return c.Delete(ctx, inventoryHostKeyPath(id))
}

// ── The workspace-wide overlay ───────────────────────────────────────────────

// ListInventoryHostKeysByHost reads one page of a workspace's approved keys,
// grouped by host name.
//
// Live, and it writes nothing: a function of the rows, like the resolution it
// sits beside, so there is no freshness to reason about and no timestamp to
// mislead anyone.
func (c *Client) ListInventoryHostKeysByHost(
	ctx context.Context, workspaceID string,
) ([]InventoryHostKeysByHost, error) {
	data, err := c.Get(ctx, inventoryWorkspacePath(workspaceID, "host-keys"))
	if err != nil {
		return nil, err
	}
	return parseInventoryHostKeysByHostList(data)
}

// ListAllInventoryHostKeysByHost pages through every host in a workspace that
// has an approved key.
//
// Worth having where the per-host list is not: this collection has one entry per
// HOST, and a `for_each` over a few hundred instances is the documented shape
// for declaring hosts.
func (c *Client) ListAllInventoryHostKeysByHost(
	ctx context.Context, workspaceID string,
) ([]InventoryHostKeysByHost, error) {
	return pageThrough(ctx, c, inventoryWorkspacePath(workspaceID, "host-keys"),
		parseInventoryHostKeysByHostList)
}

// ── Internal helpers ─────────────────────────────────────────────────────────

func inventoryHostKeysPath(hostID string) string {
	return "/api/v1/inventory-hosts/" + url.PathEscape(hostID) + "/keys"
}

func inventoryHostKeyPath(id string) string {
	return "/api/v1/inventory-host-keys/" + url.PathEscape(id)
}

func parseInventoryHostKey(body []byte) (*InventoryHostKey, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse inventory-host-key response: %w", err)
	}
	return inventoryHostKeyFromResource(res), nil
}

func parseInventoryHostKeyList(body []byte) ([]InventoryHostKey, error) {
	resources, err := ParseResourceList(body)
	if err != nil {
		return nil, fmt.Errorf("parse inventory-host-key list: %w", err)
	}
	out := make([]InventoryHostKey, 0, len(resources))
	for i := range resources {
		out = append(out, *inventoryHostKeyFromResource(&resources[i]))
	}
	return out, nil
}

func inventoryHostKeyFromResource(res *Resource) *InventoryHostKey {
	return &InventoryHostKey{
		ID:              res.ID,
		HostID:          GetRelationshipID(res, "host"),
		ApprovedHostKey: approvedHostKeyFromAttrs(res),
	}
}

func approvedHostKeyFromAttrs(res *Resource) ApprovedHostKey {
	return ApprovedHostKey{
		KeyType:   GetStringAttr(res, "key-type"),
		PublicKey: GetStringAttr(res, "public-key"),
		Marker:    GetStringAttr(res, "marker"),
		CreatedAt: GetStringAttr(res, "created-at"),
		UpdatedAt: GetStringAttr(res, "updated-at"),
	}
}

// parseInventoryHostKeysByHostList reads the overlay.
//
// Its `keys` attribute is a list of ATTRIBUTE objects rather than of resources
// -- no id, no relationships -- so it is unmarshalled straight into the typed
// slice rather than walked as a map[string]any. Hand-walking is how a field
// nothing assigns ships permanently empty with tests passing on both sides.
func parseInventoryHostKeysByHostList(body []byte) ([]InventoryHostKeysByHost, error) {
	resources, err := ParseResourceList(body)
	if err != nil {
		return nil, fmt.Errorf("parse inventory-host-keys-by-host list: %w", err)
	}
	out := make([]InventoryHostKeysByHost, 0, len(resources))
	for i := range resources {
		entry := InventoryHostKeysByHost{HostName: resources[i].ID}
		raw, ok := resources[i].Attributes["keys"]
		if ok && len(raw) > 0 && string(raw) != "null" {
			var keys []ApprovedHostKey
			if err := json.Unmarshal(raw, &keys); err != nil {
				return nil, fmt.Errorf("parse approved host keys for %q: %w",
					resources[i].ID, err)
			}
			entry.Keys = keys
		}
		out = append(out, entry)
	}
	return out, nil
}
