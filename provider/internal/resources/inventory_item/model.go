// Package inventory_item implements the terrapod_inventory_item resource
// (#1968): one ansible host, declared by the Terraform that manages it.
//
// This is the "inventory from the managing Terraform" source of #1967's
// ordered set. The managing configuration declares the hosts rather than
// Terrapod inferring them from state, which is why there is no resource-type
// to host mapping anywhere in the platform: `for_each` over the instances, a
// conditional, a module output and real dependency ordering already belong to
// the practitioner, and are more expressive than any table Terrapod could
// ship. A resource type is never "not supported yet", because Terrapod is not
// the one reading the state.
//
// API Contract (Terrapod API <-> Terraform Provider), via go-terrapod:
//
//	JSON:API type: "inventory-items"
//	ID format: "invitem-<uuid>"
//	Create:  POST   /api/v1/workspaces/{workspace_id}/inventory-items
//	Read:    GET    /api/v1/inventory-items/{id}
//	Update:  PATCH  /api/v1/inventory-items/{id}
//	Delete:  DELETE /api/v1/inventory-items/{id}
//
// Attribute mapping (JSON:API attribute -> Terraform schema attribute):
//
//	relationships.workspace -> workspace_id (string, required, RequiresReplace)
//	"name"                  -> name         (string, required)
//	"address"               -> address      (string, optional)
//	"groups"                -> groups       (set of strings, optional)
//	"vars"                  -> vars         (map of strings, optional)
//
// Read-only:
//
//	"created-at" -> created_at (string, computed)
//	"updated-at" -> updated_at (string, computed)
//
// # One resource per host
//
// A `for_each` over five hundred instances is five hundred resources in state,
// and that is the intent rather than a cost to engineer away: per-host drift
// detection is the point, and anything coarser is inflexible. The Terraform
// finishes long before the ansible that reads the inventory, so the apply is
// not where the time goes.
//
// # The provider needs no credentials inside a run
//
// A runner token may manage the inventory items of its own run's workspace,
// which is the implicit grant that lets `provider "terrapod" {}` work with
// nothing in it. The grant is bound to the apply phase for writes -- a plan
// reads inventory to diff it, and only an apply declares one.
//
// # Collections are Optional and NOT Computed, deliberately
//
// `groups` and `vars` belong wholly to the declaring configuration: nothing
// else writes one host's membership or variables. So removing the attribute
// means "this host is in no groups", and Update sends the clearing form rather
// than omitting the attribute.
//
// That is the opposite choice from `terrapod_workspace.remote_state_consumers`,
// which is Optional + Computed because a standalone resource may own the set
// instead -- there, leaving it null has to mean "not managed here". There is no
// alternative writer of one inventory item's groups, so buying that reading
// would cost the ability to clear them and buy nothing.
//
// Import: by item ID ("invitem-<uuid>"). The workspace comes back on the read,
// so an imported item needs no further configuration to plan clean.
package inventory_item

import (
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type inventoryItemModel struct {
	ID          types.String `tfsdk:"id"`
	WorkspaceID types.String `tfsdk:"workspace_id"`

	Name    types.String `tfsdk:"name"`
	Address types.String `tfsdk:"address"`
	Groups  types.Set    `tfsdk:"groups"`
	Vars    types.Map    `tfsdk:"vars"`

	CreatedAt types.String `tfsdk:"created_at"`
	UpdatedAt types.String `tfsdk:"updated_at"`
}
