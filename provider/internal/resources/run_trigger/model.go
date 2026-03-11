// Package run_trigger implements the terrapod_run_trigger resource.
//
// API Contract (Terrapod API ↔ Terraform Provider):
//
//	JSON:API type: "run-triggers"
//	ID prefix: "rt-"
//	Create:  POST   /api/v2/workspaces/{workspace_id}/run-triggers
//	Read:    GET    /api/v2/run-triggers/{id}
//	Delete:  DELETE /api/v2/run-triggers/{id}
//	No update — immutable resource (delete + recreate).
//
// Attribute mapping:
//
//	"workspace-name"  → workspace_name  (string, computed — destination workspace)
//	"sourceable-name" → sourceable_name (string, computed — source workspace)
//
// Relationships (used at create time):
//
//	"sourceable" → source_workspace_id (string, required, forces new)
//
// The workspace_id attribute identifies the destination workspace.
//
// Import: by run trigger ID.
package run_trigger

import (
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type runTriggerModel struct {
	ID                types.String `tfsdk:"id"`
	WorkspaceID       types.String `tfsdk:"workspace_id"`
	SourceWorkspaceID types.String `tfsdk:"source_workspace_id"`
	WorkspaceName     types.String `tfsdk:"workspace_name"`
	SourceableName    types.String `tfsdk:"sourceable_name"`
	CreatedAt         types.String `tfsdk:"created_at"`
}
