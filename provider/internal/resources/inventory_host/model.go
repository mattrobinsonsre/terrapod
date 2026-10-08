// Package inventory_host implements the terrapod_inventory_host resource
// (#1968): one host in a workspace's ansible inventory.
//
// API Contract (Terrapod API ↔ Terraform Provider):
//
//	JSON:API type: "inventory-hosts"
//	ID prefix:     "invhost-"
//	Create: POST   /api/v1/workspaces/{workspace_id}/inventory/hosts
//	Read:   GET    /api/v1/inventory-hosts/{id}
//	Update: PATCH  /api/v1/inventory-hosts/{id}
//	Delete: DELETE /api/v1/inventory-hosts/{id}
//
// A host has a name and nothing else: `ansible_host` is a variable like any
// other (terrapod_inventory_host_var), because two homes for one value is one
// home too many. Its groups are terrapod_inventory_host_group rows, so a
// second concern can put this host in a group without owning the host.
//
// The server also reports a group count and a variable count, and this resource
// deliberately does NOT carry them. They move when some OTHER resource creates
// a membership or a variable, so holding them here would make a host show drift
// because something else was declared correctly.
//
// `for_each` over the instances a configuration creates — or over the ones a
// configuration of nothing but data sources finds — is the documented shape,
// and gives each host its own drift detection.
//
// Import: by host id.
package inventory_host

import "github.com/hashicorp/terraform-plugin-framework/types"

type inventoryHostModel struct {
	ID          types.String `tfsdk:"id"`
	WorkspaceID types.String `tfsdk:"workspace_id"`
	Name        types.String `tfsdk:"name"`
	CreatedAt   types.String `tfsdk:"created_at"`
	UpdatedAt   types.String `tfsdk:"updated_at"`
}
