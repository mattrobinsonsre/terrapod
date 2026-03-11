// Package notification_configuration implements the terrapod_notification_configuration resource.
//
// API Contract (Terrapod API ↔ Terraform Provider):
//
//	JSON:API type: "notification-configurations"
//	ID prefix: "nc-"
//	Create:  POST   /api/v2/workspaces/{workspace_id}/notification-configurations
//	Read:    GET    /api/v2/notification-configurations/{id}
//	Update:  PATCH  /api/v2/notification-configurations/{id}
//	Delete:  DELETE /api/v2/notification-configurations/{id}
//
// Attribute mapping:
//
//	"name"             → name             (string, required)
//	"destination-type" → destination_type (string, required, forces new: "generic", "slack", "email")
//	"url"              → url              (string, optional, required for generic/slack)
//	"token"            → token            (string, optional, write-only, sensitive)
//	"enabled"          → enabled          (bool, optional, default false)
//	"triggers"         → triggers         (list of strings, optional)
//	"email-addresses"  → email_addresses  (list of strings, optional, for email type)
//
// Read-only:
//
//	"has-token"            → has_token           (bool, computed)
//	"delivery-responses"   → (not exposed in TF)
//	"created-at"           → created_at          (string, computed)
//	"updated-at"           → updated_at          (string, computed)
//
// Import: by notification configuration ID.
package notification_configuration

import (
	"github.com/hashicorp/terraform-plugin-framework/types"
)

type notificationConfigModel struct {
	ID          types.String `tfsdk:"id"`
	WorkspaceID types.String `tfsdk:"workspace_id"`

	Name            types.String `tfsdk:"name"`
	DestinationType types.String `tfsdk:"destination_type"`
	URL             types.String `tfsdk:"url"`
	Token           types.String `tfsdk:"token"`
	Enabled         types.Bool   `tfsdk:"enabled"`
	Triggers        types.List   `tfsdk:"triggers"`
	EmailAddresses  types.List   `tfsdk:"email_addresses"`

	HasToken  types.Bool   `tfsdk:"has_token"`
	CreatedAt types.String `tfsdk:"created_at"`
	UpdatedAt types.String `tfsdk:"updated_at"`
}
