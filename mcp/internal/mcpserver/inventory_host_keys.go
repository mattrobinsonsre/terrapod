package mcpserver

import (
	"context"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// registerInventoryHostKeys adds the approved-SSH-host-key tools (#2592): the
// eighth inventory structure.
//
// # What the trust set is, and why Terrapod holds it
//
// Ansible has no inventory-level model of an approved host key. Its own
// `known_hosts` module manages a file on a MANAGED node, and host-key checking
// on the controller is a boolean plus whatever `known_hosts` file the process
// finds — so "this host is trusted to present these keys" has nowhere to live in
// an inventory, and an operator's only two answers are to turn checking off or
// to ship a file out of band. Terrapod declares the set as data and the runner
// renders it to a `known_hosts` file for OpenSSH, which is the component that
// actually decides.
//
// So an agent asked why a configure cannot reach a host, or asked to trust a
// host it has just provisioned, is reading and writing this.
//
// # Declare EVERY key a host offers
//
// A host offers one key per algorithm it is configured for, and a client picks
// by its own `HostKeyAlgorithms` preference. A trust set holding one key per
// algorithm family therefore refuses the host whenever the two disagree, which
// is why nothing here picks one — declare them all.
//
// Two keys of ONE algorithm are also allowed, and that is what makes a rotation
// safe rather than a window of risk: declare the incoming key and mark the
// outgoing one `revoked`, so a replay of the old key is REFUSED rather than
// silently treated as an unknown host. The row's identity is the host, the
// algorithm and the blob, which is what lets both exist at once.
//
// # Nothing here is secret
//
// A host's public key is public, so `public_key` is returned in full — unlike a
// sensitive inventory variable, which reads back as `***`. Masking it would hide
// the one value an operator needs to read at the moment the feature exists for,
// which is a host presenting something else.
//
// # The marker is the only mutable thing about a key
//
// The algorithm and the blob are the row's identity, so changing either means a
// DIFFERENT key: delete the entry and declare the new one. The API refuses a
// patch that supplies either, and so does
// `terrapod_inventory_host_key_declare` — it declares a key, it does not edit
// one. Allowing them to be edited in place would let what a host is trusted to
// present be rewritten while the row keeps its id, which is the one audit trail
// a trust anchor should not have.
func registerInventoryHostKeys(s *mcp.Server, c *terrapod.Client) {
	// ── terrapod_inventory_host_keys ─────────────────────────────────
	type keysIn struct {
		WorkspaceID string `json:"workspace_id" jsonschema:"the workspace id (ws-...) whose approved host keys to read"`
	}
	type keysOut struct {
		// Keyed on the host NAME, not its id, because that is what
		// terrapod_inventory_resolved is keyed on and the two are meant to be
		// read side by side.
		Hosts map[string][]terrapod.ApprovedHostKey `json:"hosts"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_inventory_host_keys",
		Description: "Read every approved SSH host key in a workspace's inventory, keyed by host NAME — the trust set a configure will hold OpenSSH to. " +
			"Read it BESIDE terrapod_inventory_resolved, which is keyed the same way: together they answer \"what would a configure target, and what is each of those hosts trusted to present\". Kept separate because the resolved document is ansible's own answer, normalised, and this is Terrapod's — folding them would leave a consumer unable to tell which half it was reading. " +
			"Each key has a `key_type` (the algorithm, as OpenSSH spells it in a known_hosts line), a `public_key` (the base64 blob ALONE, no algorithm prefix and no host), and an optional `marker`. " +
			"`marker` is `cert-authority` or `revoked`, WITHOUT the leading '@' — Terrapod adds that when it renders the line — and empty means an ordinary entry. A `revoked` key is one the host must NOT present: it stays declared so a replay of a retired key is refused rather than looking like an unknown host. " +
			"A host legitimately appears with SEVERAL keys, and with two of ONE algorithm while it is being rotated. Do not read a second key of the same algorithm as a duplicate; a client picks by its own preference, so declaring all of them is the point. " +
			"`public_key` is NOT masked, deliberately: a host's public key is public, and hiding it would hide the one value needed when a host presents something else. " +
			"A host appearing in a VCS-sourced inventory file only is ABSENT here and can hold no approved key — it has no Terrapod row to hang one on, exactly as it can hold no Terrapod-declared variable. " +
			"A host present with an empty list holds no approved key. Under ssh_host_key_policy strict that means a configure cannot reach it at all; under accept-new it is reachable on FIRST contact and refused once its key changes, which is the point of that policy. " +
			"LIVE, and it writes nothing: a function of the rows, so there is no freshness to reason about and no timestamp here to mislead anyone. " +
			"The ids needed to change or remove a key are not on this view — list one host's keys with terrapod_inventory_detail, which carries them.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in keysIn) (*mcp.CallToolResult, keysOut, error) {
		// Initialised BEFORE the first return, not just on the success path.
		// `null` fails the derived output schema, so a zero-value keysOut on an
		// error path costs the agent the tool's own error message and hands it a
		// protocol failure instead -- which says nothing about what went wrong.
		// Measured: a 404 from the API surfaced as
		// `validating /properties/hosts: ... has type "null", want "object"`.
		out := keysOut{Hosts: map[string][]terrapod.ApprovedHostKey{}}
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), out, nil
		}
		byHost, err := c.ListAllInventoryHostKeysByHost(ctx, in.WorkspaceID)
		if err != nil {
			return errResult(err), out, nil
		}
		for _, entry := range byHost {
			out.Hosts[entry.HostName] = orEmptyApprovedKeys(entry.Keys)
		}
		return nil, out, nil
	})

	// ── terrapod_inventory_host_key_declare ──────────────────────────
	type declareIn struct {
		HostID    string `json:"host_id" jsonschema:"the host id (invhost-...) this key is approved for"`
		KeyType   string `json:"key_type" jsonschema:"the algorithm, spelled as OpenSSH spells it in the second field of a known_hosts line: ssh-ed25519, ecdsa-sha2-nistp256, ecdsa-sha2-nistp384, ecdsa-sha2-nistp521, ssh-rsa, rsa-sha2-256 or rsa-sha2-512. Validated against that closed set, because a typo in a trust anchor silently never matches and reads as the host having rotated its key"`
		PublicKey string `json:"public_key" jsonschema:"the base64 blob ALONE -- the THIRD field of a known_hosts line, with no algorithm prefix and no host. A whole line is refused. The blob's own embedded algorithm name is checked against key_type; note an rsa-sha2-256 or rsa-sha2-512 key embeds 'ssh-rsa', because those name signature algorithms over an RSA key"`
		Marker    string `json:"marker,omitempty" jsonschema:"cert-authority or revoked, WITHOUT the leading '@'. Omit for an ordinary entry. Supplying it for a key that is already declared CHANGES its marker, which is how a key is revoked by value rather than by id"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_inventory_host_key_declare",
		Description: "Declare one SSH host key a host is approved to present. Idempotent: declaring a key the host already has under that algorithm sets its `marker` to what you passed rather than failing, so \"this key, with this marker\" is a statement you can make without knowing what is already there. " +
			"Declare EVERY key the host offers, not one per algorithm family. A client picks by its own HostKeyAlgorithms preference, so a trust set holding one key per family refuses the host whenever the two disagree. " +
			"To ROTATE a key safely: declare the incoming key, then declare the outgoing one again with `marker: revoked` (or mark it by id with terrapod_inventory_host_key_mark). Two keys of one algorithm are allowed precisely so that both can be declared at once — and keeping the old one declared-and-revoked is what makes a replay of it REFUSED rather than indistinguishable from an unknown host. Do NOT simply remove the old key: that leaves it unknown rather than refused. " +
			"`key_type` and `public_key` are the key's IDENTITY and cannot be edited. To replace a key, remove the entry with terrapod_inventory_remove and declare the new one, so the change is visible as what it is. " +
			"Read the host keys a host actually presents with the terrapod_ssh_host_keys Terraform data source, or `ssh-keyscan`; Terrapod cannot learn them for you, because it has never connected to the host. Confirm a scanned key out of band before declaring it — a key read over an untrusted network is the thing this is meant to protect against. " +
			"A host declared only in a VCS-sourced inventory file cannot hold a key: declare the host with terrapod_inventory_host_declare first. " +
			"Requires inventory write on the workspace.",
		Annotations: mutating,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in declareIn) (*mcp.CallToolResult, *terrapod.InventoryHostKey, error) {
		switch {
		case in.HostID == "":
			return errText("host_id is required: the host id (invhost-...) this key is approved for"), nil, nil
		case in.KeyType == "":
			return errText("key_type is required, e.g. ssh-ed25519"), nil, nil
		case in.PublicKey == "":
			return errText("public_key is required: the base64 blob alone, without the algorithm prefix or the host"), nil, nil
		}

		key, err := c.CreateInventoryHostKey(ctx, in.HostID, terrapod.CreateInventoryHostKeyRequest{
			KeyType: in.KeyType, PublicKey: in.PublicKey, Marker: in.Marker,
		})
		if err == nil {
			return nil, key, nil
		}
		if !isConflict(err) {
			return errResult(err), nil, nil
		}

		// Already declared. The only thing left to change is the marker --
		// `key_type` and `public_key` are what matched -- so find the row and
		// set it, which is what makes "declare this key revoked" work whether
		// or not it was declared before. The listing is scoped to the one host.
		existing, lerr := c.ListInventoryHostKeys(ctx, in.HostID)
		if lerr != nil {
			return errResult(lerr), nil, nil
		}
		id, ok := findHostKeyID(existing, in.KeyType, in.PublicKey)
		if !ok {
			// The conflict was not this key. Report the original error rather
			// than inventing an explanation for it.
			return errResult(err), nil, nil
		}
		marker := in.Marker
		key, err = c.UpdateInventoryHostKey(ctx, id,
			terrapod.UpdateInventoryHostKeyRequest{Marker: &marker})
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, key, nil
	})

	// ── terrapod_inventory_host_key_mark ─────────────────────────────
	type markIn struct {
		KeyID  string `json:"key_id" jsonschema:"the approved key's id (invhkey-...), from terrapod_inventory_detail on its host"`
		Marker string `json:"marker,omitempty" jsonschema:"cert-authority or revoked, WITHOUT the leading '@'. OMIT IT TO CLEAR the marker and make this an ordinary entry -- this tool always writes the marker, so an omitted value removes one rather than leaving it alone"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_inventory_host_key_mark",
		Description: "Set or clear one approved host key's `marker`, by id. The marker is the ONLY mutable thing about a key: the algorithm and the blob are its identity, so changing either is a different key and means removing this entry and declaring that one. " +
			"`revoked` is the one to reach for when retiring a key: the entry stays declared and the key is REFUSED, so a replay of it is rejected rather than looking like an unknown host. Removing the entry instead leaves the old key merely unknown, which is weaker. " +
			"`cert-authority` says this key is a CA that signs host certificates rather than a host's own key, so every host it has signed is trusted without being listed. " +
			"OMITTING `marker` CLEARS it, making the key an ordinary entry — this tool always writes the field, so there is no way to call it as a no-op. " +
			"Get the id from terrapod_inventory_detail on the host; the workspace-wide terrapod_inventory_host_keys view does not carry ids. " +
			"Requires inventory write on the workspace.",
		Annotations: mutating,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in markIn) (*mcp.CallToolResult, *terrapod.InventoryHostKey, error) {
		if in.KeyID == "" {
			return errText("key_id is required: an approved key's id (invhkey-...)"), nil, nil
		}
		marker := in.Marker
		key, err := c.UpdateInventoryHostKey(ctx, in.KeyID,
			terrapod.UpdateInventoryHostKeyRequest{Marker: &marker})
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, key, nil
	})
}

// findHostKeyID locates an already-declared key by its identity -- the
// algorithm and the blob, which is what the server's unique constraint is on.
//
// Matched on BOTH, not on the algorithm alone: a host holding two keys of one
// algorithm is the mid-rotation state this table exists to be able to hold, so
// matching on the algorithm would re-mark whichever of the two came back first.
func findHostKeyID(keys []terrapod.InventoryHostKey, keyType, publicKey string) (string, bool) {
	for i := range keys {
		if keys[i].KeyType == keyType && keys[i].PublicKey == publicKey {
			return keys[i].ID, true
		}
	}
	return "", false
}

// orEmptyApprovedKeys replaces a nil slice with an empty one: `null` fails the
// tool's derived output schema and costs the agent the whole result, where an
// empty list is a real answer -- a host that is declared and approved to
// present nothing.
func orEmptyApprovedKeys(v []terrapod.ApprovedHostKey) []terrapod.ApprovedHostKey {
	if v == nil {
		return []terrapod.ApprovedHostKey{}
	}
	return v
}

// orEmptyHostKeys is the same for the addressable rows, which
// terrapod_inventory_detail returns for one host.
func orEmptyHostKeys(v []terrapod.InventoryHostKey) []terrapod.InventoryHostKey {
	if v == nil {
		return []terrapod.InventoryHostKey{}
	}
	return v
}
