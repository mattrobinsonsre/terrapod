// Package run_task implements the terrapod_run_task resource.
//
// API Contract (Terrapod API ↔ Terraform Provider):
//
//	JSON:API type: "run-tasks"
//	ID prefix: "task-"
//	Create:  POST   /api/v2/workspaces/{workspace_id}/run-tasks
//	Read:    GET    /api/v2/run-tasks/{id}
//	Update:  PATCH  /api/v2/run-tasks/{id}
//	Delete:  DELETE /api/v2/run-tasks/{id}
//
// Attribute mapping:
//
//	"name"              → name              (string, required)
//	"url"               → url               (string, required)
//	"enabled"           → enabled           (bool, optional, default true)
//	"stage"             → stage             (string, required: "pre_plan", "post_plan", "pre_apply")
//	"enforcement-level" → enforcement_level (string, required: "mandatory", "advisory")
//	"hmac-key"          → hmac_key          (string, optional, write-only, sensitive)
//
// Read-only:
//
//	"has-hmac-key" → has_hmac_key (bool, computed)
//	"created-at"   → created_at   (string, computed)
//	"updated-at"   → updated_at   (string, computed)
//
// Import: by run task ID.
package run_task

import (
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type runTaskModel struct {
	ID          types.String `tfsdk:"id"`
	WorkspaceID types.String `tfsdk:"workspace_id"`

	Name             types.String `tfsdk:"name"`
	URL              types.String `tfsdk:"url"`
	Enabled          types.Bool   `tfsdk:"enabled"`
	Stage            types.String `tfsdk:"stage"`
	EnforcementLevel types.String `tfsdk:"enforcement_level"`
	HMACKey          types.String `tfsdk:"hmac_key"`

	HasHMACKey types.Bool   `tfsdk:"has_hmac_key"`
	CreatedAt  types.String `tfsdk:"created_at"`
	UpdatedAt  types.String `tfsdk:"updated_at"`
}
