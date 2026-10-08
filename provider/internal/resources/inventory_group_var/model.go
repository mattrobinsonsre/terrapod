// Package inventory_group_var implements the terrapod_inventory_group_var
// resource (#1968): one entry in a group's `group_vars`.
//
// API Contract (Terrapod API ↔ Terraform Provider):
//
//	JSON:API type: "inventory-group-vars"
//	ID prefix:     "invgvar-"
//	Create: POST   /api/v1/inventory-groups/{group_id}/vars
//	Read:   GET    /api/v1/inventory-group-vars/{id}
//	Update: PATCH  /api/v1/inventory-group-vars/{id}
//	Delete: DELETE /api/v1/inventory-group-vars/{id}
//
// A variable is a row rather than a map entry on its parent so that it has
// exactly one writer: a second concern can add a group variable without taking
// ownership of every other variable that group has.
//
// `key` is mutable. It need not have been — a rename could have been a delete
// and a create — but the row carries three other fields and making a
// practitioner rebuild them to correct a typo is work with nothing behind it.
//
// For a variable that applies to every host, use
// terrapod_inventory_global_var: `all` cannot be declared as a group, because
// it is ansible's own root and the rendered document's root key.
//
// The same rules apply to terrapod_inventory_host_var and
// terrapod_inventory_global_var; the three schemas are held in step by a parity
// gate in internal/provider.
//
// Import: by variable id.
package inventory_group_var

import "github.com/hashicorp/terraform-plugin-framework/types"

type inventoryGroupVarModel struct {
	ID         types.String `tfsdk:"id"`
	GroupID    types.String `tfsdk:"group_id"`
	Key        types.String `tfsdk:"key"`
	Value      types.String `tfsdk:"value"`
	Structured types.Bool   `tfsdk:"structured"`
	Sensitive  types.Bool   `tfsdk:"sensitive"`
	CreatedAt  types.String `tfsdk:"created_at"`
	UpdatedAt  types.String `tfsdk:"updated_at"`
}
