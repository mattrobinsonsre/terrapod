// Package inventory_settings implements the terrapod_inventory_settings
// resource (#1968): a workspace's inventory configuration — whether the rows
// declared through the API contribute to the resolution, and the VCS directory
// merged with them.
//
// API Contract (Terrapod API ↔ Terraform Provider):
//
//	JSON:API type: "inventory-settings"
//	ID:            the workspace id ("ws-<uuid>") — one inventory per
//	               workspace, so there is no surrogate key
//	Read:   GET    /api/v1/workspaces/{workspace_id}/inventory/settings
//	Write:  PUT    /api/v1/workspaces/{workspace_id}/inventory/settings
//	Delete: DELETE /api/v1/workspaces/{workspace_id}/inventory/settings
//
// Create and Update both PUT, because PUT is a full replace and Terraform
// always knows the whole intended state. The SDK's PATCH shape exists for a
// caller changing one field without knowing the rest, which a provider never
// is.
//
// The VCS binding here is NOT the workspace's terraform binding. Even in one
// repository the root directories differ, and a configure-only workspace has no
// terraform binding at all. It points at a DIRECTORY, which ansible reads as
// one source in lexical filename order, so one binding already carries
// arbitrarily many files and ordering within it is the operator's business via
// filenames.
//
// Import: by workspace id.
package inventory_settings

import "github.com/hashicorp/terraform-plugin-framework/types"

type inventorySettingsModel struct {
	ID               types.String `tfsdk:"id"`
	WorkspaceID      types.String `tfsdk:"workspace_id"`
	IncludePlatform  types.Bool   `tfsdk:"include_platform"`
	VCSConnectionID  types.String `tfsdk:"vcs_connection_id"`
	RepoURL          types.String `tfsdk:"repo_url"`
	Branch           types.String `tfsdk:"branch"`
	WorkingDirectory types.String `tfsdk:"working_directory"`
	IgnorePaths      types.List   `tfsdk:"ignore_paths"`
	CreatedAt        types.String `tfsdk:"created_at"`
	UpdatedAt        types.String `tfsdk:"updated_at"`
}
