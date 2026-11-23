// Package inventory_host_keys implements the terrapod_inventory_host_keys
// resource (#2592): the set of SSH host keys one inventory host is approved to
// present.
//
// API Contract (Terrapod API ↔ Terraform Provider):
//
//	JSON:API type: "inventory-host-keys"
//	ID prefix:     "invhkey-"
//	Create: POST   /api/v1/inventory-hosts/{host_id}/keys     (one call per key)
//	Read:   GET    /api/v1/inventory-hosts/{host_id}/keys
//	Update: PATCH  /api/v1/inventory-host-keys/{id}           (marker only)
//	Delete: DELETE /api/v1/inventory-host-keys/{id}
//
// # One resource per HOST, with one `key` block per key
//
// Unlike every sibling inventory resource, this one is an AGGREGATE: it owns a
// host's whole trust set rather than one row. That is what the repeated `key`
// block means, and it has two consequences worth stating before anyone meets
// them.
//
// A host exposes one key per algorithm it is configured for, and a client picks
// by its own `HostKeyAlgorithms` preference — so a trust set that kept one key
// per algorithm family would refuse the host whenever the two disagreed.
// Declaring all of them is the point, and a `key` block per key is what says so
// in one place.
//
// And a key appearing on the host out of band is DRIFT, removed by the next
// apply. That is the behaviour a declared trust anchor wants: the alternative is
// a key nobody approved quietly becoming trusted. The cost is that two
// terrapod_inventory_host_keys resources naming one host will fight, each
// removing the other's keys — one resource per host, always.
//
// # The blocks carry no computed attributes, deliberately
//
// A set element's identity is its whole value, so a Computed attribute inside
// one leaves the planned element unknown and Terraform can no longer match a
// configuration element to a state element — which is how a set of nested
// objects acquires a permanent diff, or an "inconsistent result after apply".
// So `key` carries only what the configuration supplies: no per-key id and no
// timestamps. Reach for the API or terrapod-mcp when you need a row's id.
//
// # `marker` is Optional and NOT Computed
//
// The server stores null for an ordinary entry and normalises an empty string
// to null, so there is no server-side value to compute: Optional+Computed would
// make an absent marker plan as unknown and then settle as null, which is a
// diff that never converges. The read projects the server's empty marker back
// to null so an absent block attribute and an unmarked row agree.
//
// Import: by HOST id, because the host is this resource's identity. The keys
// come back from the read.
package inventory_host_keys

import "github.com/hashicorp/terraform-plugin-framework/types"

type inventoryHostKeysModel struct {
	ID     types.String `tfsdk:"id"`
	HostID types.String `tfsdk:"host_id"`
	// Keys is the `key` blocks, as a SET: a trust set has no order, and a list
	// would plan a diff every time two blocks were swapped in the
	// configuration.
	Keys types.Set `tfsdk:"key"`
}

// hostKeyBlockModel is one `key` block.
type hostKeyBlockModel struct {
	KeyType   types.String `tfsdk:"key_type"`
	PublicKey types.String `tfsdk:"public_key"`
	Marker    types.String `tfsdk:"marker"`
}
