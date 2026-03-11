// Package workspace implements the terrapod_workspace resource.
//
// API Contract (Terrapod API ↔ Terraform Provider):
//
//	JSON:API type: "workspaces"
//	ID prefix: "ws-"
//	Create:  POST   /api/v2/organizations/default/workspaces
//	Read:    GET    /api/v2/workspaces/{id}
//	Update:  PATCH  /api/v2/workspaces/{id}
//	Delete:  DELETE /api/v2/workspaces/{id}
//	By name: GET    /api/v2/organizations/default/workspaces/{name}
//	List:    GET    /api/v2/organizations/default/workspaces
//
// Attribute mapping (JSON:API attribute → Terraform schema attribute):
//
//	"name"                              → name                (string, required, supports rename)
//	"execution-mode"                    → execution_mode      (string, optional, default "local")
//	"auto-apply"                        → auto_apply          (bool,   optional, default false)
//	"execution-backend"                 → execution_backend   (string, optional, default "terraform")
//	"terraform-version"                 → terraform_version   (string, optional)
//	"working-directory"                 → working_directory   (string, optional)
//	"resource-cpu"                      → resource_cpu        (string, optional, default "1")
//	"resource-memory"                   → resource_memory     (string, optional, default "2Gi")
//	"labels"                            → labels              (map,    optional)
//	"vcs-repo-url"                      → vcs_repo_url        (string, optional)
//	"vcs-branch"                        → vcs_branch          (string, optional)
//	"vcs-working-directory"             → vcs_working_directory (string, optional)
//	"agent-pool-id"                     → agent_pool_id       (string, optional)
//	"drift-detection-enabled"           → drift_detection_enabled (bool, optional)
//	"drift-detection-interval-seconds"  → drift_detection_interval_seconds (int, optional)
//
// Read-only attributes:
//
//	"owner-email"                       → owner_email         (string, computed)
//	"drift-status"                      → drift_status        (string, computed)
//	"drift-last-checked-at"             → drift_last_checked_at (string, computed)
//	"locked"                            → locked              (bool,   computed)
//	"created-at"                        → created_at          (string, computed)
//	"updated-at"                        → updated_at          (string, computed)
//
// Relationships:
//
//	"vcs-connection" → vcs_connection_id (string, optional, to-one)
//
// Import: by workspace name (resolved via GET by-name endpoint).
package workspace

import (
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// workspaceModel maps the Terraform schema to Go types.
type workspaceModel struct {
	ID types.String `tfsdk:"id"`

	// Writable attributes
	Name                          types.String `tfsdk:"name"`
	ExecutionMode                 types.String `tfsdk:"execution_mode"`
	AutoApply                     types.Bool   `tfsdk:"auto_apply"`
	ExecutionBackend              types.String `tfsdk:"execution_backend"`
	TerraformVersion              types.String `tfsdk:"terraform_version"`
	WorkingDirectory              types.String `tfsdk:"working_directory"`
	ResourceCPU                   types.String `tfsdk:"resource_cpu"`
	ResourceMemory                types.String `tfsdk:"resource_memory"`
	Labels                        types.Map    `tfsdk:"labels"`
	VCSRepoURL                    types.String `tfsdk:"vcs_repo_url"`
	VCSBranch                     types.String `tfsdk:"vcs_branch"`
	VCSWorkingDirectory           types.String `tfsdk:"vcs_working_directory"`
	VCSConnectionID               types.String `tfsdk:"vcs_connection_id"`
	AgentPoolID                   types.String `tfsdk:"agent_pool_id"`
	DriftDetectionEnabled         types.Bool   `tfsdk:"drift_detection_enabled"`
	DriftDetectionIntervalSeconds types.Int64  `tfsdk:"drift_detection_interval_seconds"`

	// Read-only attributes
	OwnerEmail         types.String `tfsdk:"owner_email"`
	DriftStatus        types.String `tfsdk:"drift_status"`
	DriftLastCheckedAt types.String `tfsdk:"drift_last_checked_at"`
	Locked             types.Bool   `tfsdk:"locked"`
	CreatedAt          types.String `tfsdk:"created_at"`
	UpdatedAt          types.String `tfsdk:"updated_at"`
}
