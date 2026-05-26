package terrapod

import (
	"context"
	"fmt"
	"net/url"
)

// User is the decoded form of a Terrapod local user. The email is
// the stable identifier on the API surface (no separate UUID) — both
// in URL paths and in the JSON:API "id" field. Password is
// write-only (HasPassword reflects whether one is configured).
type User struct {
	Email       string `json:"email"`
	DisplayName string `json:"display-name,omitempty"`
	IsActive    bool   `json:"is-active"`
	HasPassword bool   `json:"has-password"`
	LastLoginAt string `json:"last-login-at,omitempty"`
	CreatedAt   string `json:"created-at,omitempty"`
	UpdatedAt   string `json:"updated-at,omitempty"`
}

// CreateUserRequest is the input shape for Client.CreateUser. Email
// is required and becomes the user's stable id. IsActive defaults to
// true on the server when omitted.
type CreateUserRequest struct {
	Email       string
	DisplayName string
	IsActive    *bool  // nil ⇒ server default (true)
	Password    string // optional — leave empty for SSO-only users
}

// UpdateUserRequest is the partial-update shape. Email is immutable;
// to "rename" a user, delete and recreate. Pointer-typed IsActive
// preserves "leave alone" semantics.
type UpdateUserRequest struct {
	DisplayName *string
	IsActive    *bool
	Password    string // non-empty ⇒ reset password
}

// CreateUser provisions a new user. Admin role required.
func (c *Client) CreateUser(ctx context.Context, req CreateUserRequest) (*User, error) {
	body, err := MarshalResource("users", userCreateAttrs(req), nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create user: %w", err)
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/users", body)
	if err != nil {
		return nil, err
	}
	return parseUser(data)
}

// GetUser reads a user by email.
func (c *Client) GetUser(ctx context.Context, email string) (*User, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/users/"+url.PathEscape(email))
	if err != nil {
		return nil, err
	}
	return parseUser(data)
}

// ListUsers returns every user. Visibility scoped by the caller's
// role (admin/audit see all).
func (c *Client) ListUsers(ctx context.Context) ([]User, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/users")
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, err
	}
	out := make([]User, 0, len(resources))
	for i := range resources {
		out = append(out, *userFromResource(&resources[i]))
	}
	return out, nil
}

// UpdateUser patches the user. Email is immutable.
func (c *Client) UpdateUser(ctx context.Context, email string, req UpdateUserRequest) (*User, error) {
	body, err := MarshalResourceWithID(email, "users", userUpdateAttrs(req))
	if err != nil {
		return nil, fmt.Errorf("marshal update user: %w", err)
	}
	data, err := c.Patch(ctx, "/api/terrapod/v1/users/"+url.PathEscape(email), body)
	if err != nil {
		return nil, err
	}
	return parseUser(data)
}

// DeleteUser removes the user. Role assignments referencing the
// user are also cleaned up by the server.
func (c *Client) DeleteUser(ctx context.Context, email string) error {
	return c.Delete(ctx, "/api/terrapod/v1/users/"+url.PathEscape(email))
}

// ── Internal helpers ─────────────────────────────────────────────────

func userCreateAttrs(req CreateUserRequest) map[string]any {
	attrs := map[string]any{
		"email": req.Email,
	}
	if req.DisplayName != "" {
		attrs["display-name"] = req.DisplayName
	}
	if req.IsActive != nil {
		attrs["is-active"] = *req.IsActive
	}
	if req.Password != "" {
		attrs["password"] = req.Password
	}
	return attrs
}

func userUpdateAttrs(req UpdateUserRequest) map[string]any {
	attrs := map[string]any{}
	if req.DisplayName != nil {
		attrs["display-name"] = *req.DisplayName
	}
	if req.IsActive != nil {
		attrs["is-active"] = *req.IsActive
	}
	if req.Password != "" {
		attrs["password"] = req.Password
	}
	return attrs
}

func parseUser(body []byte) (*User, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse user response: %w", err)
	}
	return userFromResource(res), nil
}

func userFromResource(res *Resource) *User {
	// The user "id" is the email — but the email attribute should
	// always be present too. Prefer the attribute, fall back to ID
	// (defensive against legacy responses).
	email := GetStringAttr(res, "email")
	if email == "" {
		email = res.ID
	}
	return &User{
		Email:       email,
		DisplayName: GetStringAttr(res, "display-name"),
		IsActive:    GetBoolAttr(res, "is-active"),
		HasPassword: GetBoolAttr(res, "has-password"),
		LastLoginAt: GetStringAttr(res, "last-login-at"),
		CreatedAt:   GetStringAttr(res, "created-at"),
		UpdatedAt:   GetStringAttr(res, "updated-at"),
	}
}
