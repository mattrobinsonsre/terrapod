// Package inventory_group_child implements the terrapod_inventory_group_child
// resource (#1968): one group nested inside another — a
// `[groupname:children]` entry.
//
// API Contract (Terrapod API ↔ Terraform Provider):
//
//	JSON:API type: "inventory-group-children"
//	ID prefix:     "invgc-"
//	Create: POST   /api/v1/inventory-groups/{parent_group_id}/children
//	Read:   GET    /api/v1/inventory-group-children/{id}
//	Delete: DELETE /api/v1/inventory-group-children/{id}
//	No update — immutable (both sides force a replacement).
//
// A group may have several parents, and the server refuses a cycle. Created
// from the parent's side, because one resource should have one code path: the
// SDK's child-side creator writes the identical row.
//
// Nesting is what carries the structure a resolved inventory's per-group host
// list does not: ansible does not flatten nesting into a group's own member
// list, so a parent whose members all arrive through a child reports none of
// its own.
//
// Import: by nesting id.
package inventory_group_child

import "github.com/hashicorp/terraform-plugin-framework/types"

type inventoryGroupChildModel struct {
	ID            types.String `tfsdk:"id"`
	ParentGroupID types.String `tfsdk:"parent_group_id"`
	ChildGroupID  types.String `tfsdk:"child_group_id"`
	CreatedAt     types.String `tfsdk:"created_at"`
}
