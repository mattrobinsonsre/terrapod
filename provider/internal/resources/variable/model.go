// Package variable implements the terrapod_variable resource.
//
// API Contract (Terrapod API ↔ Terraform Provider):
//
//	JSON:API type: "vars"
//	ID prefix: "var-"
//	Create:  POST   /api/v2/workspaces/{workspace_id}/vars
//	Read:    GET    /api/v2/workspaces/{workspace_id}/vars  (list, filter by key)
//	Update:  PATCH  /api/v2/workspaces/{workspace_id}/vars/{id}
//	Delete:  DELETE /api/v2/workspaces/{workspace_id}/vars/{id}
//
// Attribute mapping (JSON:API → Terraform):
//
//	"key"         → key         (string, required)
//	"value"       → value       (string, optional, sensitive when sensitive=true)
//	"category"    → category    (string, required: "terraform" or "env")
//	"hcl"         → hcl         (bool, optional)
//	"sensitive"   → sensitive   (bool, optional)
//	"description" → description (string, optional)
//
// Read-only:
//
//	"version-id"  → version_id  (string, computed)
//	"created-at"  → created_at  (string, computed)
//	"updated-at"  → updated_at  (string, computed)
//
// Note: When sensitive=true, the API returns value=null. The provider stores
// the configured value in state and never reads it back from the API.
//
// Import: workspace_id/variable_id
package variable

import (
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type variableModel struct {
	ID          types.String `tfsdk:"id"`
	WorkspaceID types.String `tfsdk:"workspace_id"`

	Key         types.String `tfsdk:"key"`
	Value       types.String `tfsdk:"value"`
	Category    types.String `tfsdk:"category"`
	HCL         types.Bool   `tfsdk:"hcl"`
	Sensitive   types.Bool   `tfsdk:"sensitive"`
	Description types.String `tfsdk:"description"`

	VersionID types.String `tfsdk:"version_id"`
	CreatedAt types.String `tfsdk:"created_at"`
	UpdatedAt types.String `tfsdk:"updated_at"`
}
