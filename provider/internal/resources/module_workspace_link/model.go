// Package module_workspace_link implements the terrapod_module_workspace_link resource.
//
// API Contract (Terrapod API <-> Terraform Provider):
//
//	JSON:API type: "workspace-links"
//	Create: POST   /api/v2/organizations/default/registry-modules/private/default/{name}/{provider}/workspace-links
//	List:   GET    /api/v2/organizations/default/registry-modules/private/default/{name}/{provider}/workspace-links
//	Delete: DELETE /api/v2/organizations/default/registry-modules/private/default/{name}/{provider}/workspace-links/{id}
//	No update -- immutable resource (delete + recreate).
//
// Attribute mapping:
//
//	"workspace-id"   -> workspace_id   (string, in create request body)
//	"workspace-name" -> workspace_name (string, computed)
//	"created-at"     -> created_at     (string, computed)
//	"created-by"     -> created_by     (string, computed)
//
// Import: by composite ID "module_name/provider_name/link_uuid".
package module_workspace_link

import (
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type moduleWorkspaceLinkModel struct {
	ID             types.String `tfsdk:"id"`
	ModuleName     types.String `tfsdk:"module_name"`
	ModuleProvider types.String `tfsdk:"module_provider"`
	WorkspaceID    types.String `tfsdk:"workspace_id"`
	WorkspaceName  types.String `tfsdk:"workspace_name"`
	CreatedAt      types.String `tfsdk:"created_at"`
	CreatedBy      types.String `tfsdk:"created_by"`
}
