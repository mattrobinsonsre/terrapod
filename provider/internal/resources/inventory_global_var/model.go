// Package inventory_global_var implements the terrapod_inventory_global_var
// resource (#1968): one entry in `group_vars/all` — a variable that applies to
// every host in a workspace's inventory.
//
// API Contract (Terrapod API ↔ Terraform Provider):
//
//	JSON:API type: "inventory-global-vars"
//	ID prefix:     "invvar-"
//	Create: POST   /api/v1/workspaces/{workspace_id}/inventory/vars
//	Read:   GET    /api/v1/inventory-global-vars/{id}
//	Update: PATCH  /api/v1/inventory-global-vars/{id}
//	Delete: DELETE /api/v1/inventory-global-vars/{id}
//
// Parented on the workspace rather than on a group, because `all` cannot BE a
// group: it is ansible's own root and the rendered document's root key, so
// terrapod_inventory_group refuses it as a name and this resource is where a
// variable under `all` goes instead.
//
// A variable is a row rather than a map entry on its parent so that it has
// exactly one writer: a second concern can add a global variable without
// taking ownership of every other one.
//
// `key` is mutable. It need not have been — a rename could have been a delete
// and a create — but the row carries three other fields and making a
// practitioner rebuild them to correct a typo is work with nothing behind it.
//
// The same rules apply to terrapod_inventory_host_var and
// terrapod_inventory_group_var; the three schemas are held in step by a parity
// gate in internal/provider.
//
// Import: by variable id.
package inventory_global_var

import "github.com/hashicorp/terraform-plugin-framework/types"

type inventoryGlobalVarModel struct {
	ID          types.String `tfsdk:"id"`
	WorkspaceID types.String `tfsdk:"workspace_id"`
	Key         types.String `tfsdk:"key"`
	Value       types.String `tfsdk:"value"`
	Structured  types.Bool   `tfsdk:"structured"`
	Sensitive   types.Bool   `tfsdk:"sensitive"`
	CreatedAt   types.String `tfsdk:"created_at"`
	UpdatedAt   types.String `tfsdk:"updated_at"`
}
