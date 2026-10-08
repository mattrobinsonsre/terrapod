// Package inventory_group implements the terrapod_inventory_group resource
// (#1968): one group in a workspace's ansible inventory.
//
// API Contract (Terrapod API ↔ Terraform Provider):
//
//	JSON:API type: "inventory-groups"
//	ID prefix:     "invgroup-"
//	Create: POST   /api/v1/workspaces/{workspace_id}/inventory/groups
//	Read:   GET    /api/v1/inventory-groups/{id}
//	Update: PATCH  /api/v1/inventory-groups/{id}
//	Delete: DELETE /api/v1/inventory-groups/{id}
//
// A group is a name. Its members are terrapod_inventory_host_group rows, its
// nesting is terrapod_inventory_group_child rows, and its variables are
// terrapod_inventory_group_var rows — each its own addressable resource, so a
// second concern can contribute to a group it does not own.
//
// `all` and `ungrouped` cannot be declared: `all` is ansible's own root and the
// rendered document's root key, and `ungrouped` is derived from the membership
// rows. Both are ansible's to produce.
//
// Like terrapod_inventory_host, this does not carry the server's member, child
// or variable counts: they move when some OTHER resource is declared, so
// holding them here would report drift for work done correctly elsewhere.
//
// Import: by group id.
package inventory_group

import "github.com/hashicorp/terraform-plugin-framework/types"

type inventoryGroupModel struct {
	ID          types.String `tfsdk:"id"`
	WorkspaceID types.String `tfsdk:"workspace_id"`
	Name        types.String `tfsdk:"name"`
	CreatedAt   types.String `tfsdk:"created_at"`
	UpdatedAt   types.String `tfsdk:"updated_at"`
}
