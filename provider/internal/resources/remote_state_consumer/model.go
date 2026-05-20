// Package remote_state_consumer implements the
// terrapod_remote_state_consumer resource — one row in the
// producer-controlled allowlist authorizing a consumer workspace's
// agent runs to read a producer workspace's state via
// terraform_remote_state (#344).
//
// API Contract (Terrapod API ↔ Terraform Provider):
//
//	JSON:API type: "remote-state-consumers"
//	ID prefix: "rsc-"
//	Create:  POST   /api/terrapod/v1/workspaces/{producer_id}/remote-state-consumers
//	Read:    GET    /api/terrapod/v1/remote-state-consumers/{id}
//	Delete:  DELETE /api/terrapod/v1/remote-state-consumers/{id}
//	No update — immutable resource (delete + recreate).
//
// Use this standalone resource when the producer workspace and the
// consumer workspace live in different Terraform configurations (the
// common cross-team / cross-config case). For the single-config GitOps
// case where the producer's resource block already exists, the set
// attribute on `terrapod_workspace` is the ergonomic choice. Both
// shapes go through the same server-side authorization: mutations
// require admin on the PRODUCER, so a consumer team cannot self-grant.
//
// Import: by edge ID.
package remote_state_consumer

import (
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type remoteStateConsumerModel struct {
	ID                    types.String `tfsdk:"id"`
	ProducerWorkspaceID   types.String `tfsdk:"producer_workspace_id"`
	ConsumerWorkspaceID   types.String `tfsdk:"consumer_workspace_id"`
	ProducerWorkspaceName types.String `tfsdk:"producer_workspace_name"`
	ConsumerWorkspaceName types.String `tfsdk:"consumer_workspace_name"`
	CreatedAt             types.String `tfsdk:"created_at"`
	CreatedBy             types.String `tfsdk:"created_by"`
}
