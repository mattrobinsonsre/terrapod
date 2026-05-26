package terrapod

import (
	"context"
	"fmt"
	"net/url"
)

// NotificationConfiguration is a per-workspace notification target —
// generic webhook, Slack, or email. The HMAC token (when used) is
// write-only.
type NotificationConfiguration struct {
	ID              string   `json:"id"`
	WorkspaceID     string   `json:"workspace-id,omitempty"`
	Name            string   `json:"name"`
	DestinationType string   `json:"destination-type"`
	URL             string   `json:"url,omitempty"`
	Enabled         bool     `json:"enabled"`
	HasToken        bool     `json:"has-token"`
	Triggers        []string `json:"triggers,omitempty"`
	EmailAddresses  []string `json:"email-addresses,omitempty"`
	CreatedAt       string   `json:"created-at,omitempty"`
	UpdatedAt       string   `json:"updated-at,omitempty"`
}

// CreateNotificationConfigurationRequest is the input shape. Token
// is write-only — sent to the server but never echoed back.
type CreateNotificationConfigurationRequest struct {
	Name            string
	DestinationType string // "generic" | "slack" | "email"
	URL             string
	Token           string
	Enabled         bool
	Triggers        []string
	EmailAddresses  []string
}

// UpdateNotificationConfigurationRequest is the partial-update shape.
// Token is empty ⇒ leave alone, non-empty ⇒ rotate.
type UpdateNotificationConfigurationRequest struct {
	Name            string
	URL             *string
	Token           string
	Enabled         *bool
	Triggers        *[]string
	EmailAddresses  *[]string
}

// CreateNotificationConfiguration creates a new notification target
// scoped to the given workspace.
func (c *Client) CreateNotificationConfiguration(ctx context.Context, workspaceID string, req CreateNotificationConfigurationRequest) (*NotificationConfiguration, error) {
	body, err := MarshalResource("notification-configurations", notificationConfigCreateAttrs(req), nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create notification-config: %w", err)
	}
	data, err := c.Post(ctx,
		fmt.Sprintf("/api/terrapod/v1/workspaces/%s/notification-configurations", url.PathEscape(workspaceID)),
		body)
	if err != nil {
		return nil, err
	}
	return parseNotificationConfiguration(data)
}

// GetNotificationConfiguration reads a config by id.
func (c *Client) GetNotificationConfiguration(ctx context.Context, id string) (*NotificationConfiguration, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/notification-configurations/"+url.PathEscape(id))
	if err != nil {
		return nil, err
	}
	return parseNotificationConfiguration(data)
}

// ListNotificationConfigurations returns all configs for a workspace.
func (c *Client) ListNotificationConfigurations(ctx context.Context, workspaceID string) ([]NotificationConfiguration, error) {
	data, err := c.Get(ctx,
		fmt.Sprintf("/api/terrapod/v1/workspaces/%s/notification-configurations", url.PathEscape(workspaceID)))
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, err
	}
	out := make([]NotificationConfiguration, 0, len(resources))
	for i := range resources {
		out = append(out, *notificationConfigFromResource(&resources[i]))
	}
	return out, nil
}

// UpdateNotificationConfiguration patches a config.
func (c *Client) UpdateNotificationConfiguration(ctx context.Context, id string, req UpdateNotificationConfigurationRequest) (*NotificationConfiguration, error) {
	body, err := MarshalResourceWithID(id, "notification-configurations", notificationConfigUpdateAttrs(req))
	if err != nil {
		return nil, fmt.Errorf("marshal update notification-config: %w", err)
	}
	data, err := c.Patch(ctx, "/api/terrapod/v1/notification-configurations/"+url.PathEscape(id), body)
	if err != nil {
		return nil, err
	}
	return parseNotificationConfiguration(data)
}

// DeleteNotificationConfiguration removes a config.
func (c *Client) DeleteNotificationConfiguration(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/terrapod/v1/notification-configurations/"+url.PathEscape(id))
}

// ── Internal helpers ─────────────────────────────────────────────────

func notificationConfigCreateAttrs(req CreateNotificationConfigurationRequest) map[string]any {
	attrs := map[string]any{
		"name":             req.Name,
		"destination-type": req.DestinationType,
		"enabled":          req.Enabled,
	}
	if req.URL != "" {
		attrs["url"] = req.URL
	}
	if req.Token != "" {
		attrs["token"] = req.Token
	}
	if req.Triggers != nil {
		attrs["triggers"] = req.Triggers
	}
	if req.EmailAddresses != nil {
		attrs["email-addresses"] = req.EmailAddresses
	}
	return attrs
}

func notificationConfigUpdateAttrs(req UpdateNotificationConfigurationRequest) map[string]any {
	attrs := map[string]any{}
	if req.Name != "" {
		attrs["name"] = req.Name
	}
	if req.URL != nil {
		attrs["url"] = *req.URL
	}
	if req.Token != "" {
		attrs["token"] = req.Token
	}
	if req.Enabled != nil {
		attrs["enabled"] = *req.Enabled
	}
	if req.Triggers != nil {
		attrs["triggers"] = *req.Triggers
	}
	if req.EmailAddresses != nil {
		attrs["email-addresses"] = *req.EmailAddresses
	}
	return attrs
}

func parseNotificationConfiguration(body []byte) (*NotificationConfiguration, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse notification-config response: %w", err)
	}
	return notificationConfigFromResource(res), nil
}

func notificationConfigFromResource(res *Resource) *NotificationConfiguration {
	return &NotificationConfiguration{
		ID:              res.ID,
		Name:            GetStringAttr(res, "name"),
		DestinationType: GetStringAttr(res, "destination-type"),
		URL:             GetStringAttr(res, "url"),
		Enabled:         GetBoolAttr(res, "enabled"),
		HasToken:        GetBoolAttr(res, "has-token"),
		Triggers:        GetListAttr(res, "triggers"),
		EmailAddresses:  GetListAttr(res, "email-addresses"),
		CreatedAt:       GetStringAttr(res, "created-at"),
		UpdatedAt:       GetStringAttr(res, "updated-at"),
	}
}
