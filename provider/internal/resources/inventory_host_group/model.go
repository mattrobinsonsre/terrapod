// Package inventory_host_group implements the terrapod_inventory_host_group
// resource (#1968): one host's membership of one group — a `[groupname]` host
// line.
//
// API Contract (Terrapod API ↔ Terraform Provider):
//
//	JSON:API type: "inventory-host-groups"
//	ID prefix:     "invhg-"
//	Create: POST   /api/v1/inventory-groups/{group_id}/hosts
//	Read:   GET    /api/v1/inventory-host-groups/{id}
//	Delete: DELETE /api/v1/inventory-host-groups/{id}
//	No update — immutable (both sides force a replacement).
//
// Many-to-many, and its own addressable row so that a second concern can put a
// host in a group without owning either side. Created from the group's side,
// because one resource should have one code path: the SDK's host-side creator
// writes the identical row and exists for a script that happens to be looping
// over a host's groups.
//
// Import: by membership id.
package inventory_host_group

import "github.com/hashicorp/terraform-plugin-framework/types"

type inventoryHostGroupModel struct {
	ID        types.String `tfsdk:"id"`
	HostID    types.String `tfsdk:"host_id"`
	GroupID   types.String `tfsdk:"group_id"`
	CreatedAt types.String `tfsdk:"created_at"`
}
