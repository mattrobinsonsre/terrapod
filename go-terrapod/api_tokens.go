package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
)

// APIToken is a user / automation authentication token (#495). Kind is one of
// "interactive", "service_bound", or "service_detached":
//
//   - interactive: a person's CLI/login token, carrying their live roles.
//   - service_bound: a service token whose effective permissions are the
//     intersection of PinnedRoles and BoundTo's live roles; it stops working
//     when that account is removed (offboarding-safe by construction).
//   - service_detached: an admin-managed token with PinnedRoles as its absolute
//     scope, bound to no user (BoundTo empty) — for critical machine-to-machine
//     automation that must survive any one person leaving.
//
// Token holds the raw secret and is populated only by CreateAPIToken and
// RotateAPIToken; every other read leaves it empty.
type APIToken struct {
	ID            string   `json:"id"`
	Token         string   `json:"token,omitempty"` // raw value — create / rotate only
	Description   string   `json:"description,omitempty"`
	Kind          string   `json:"kind"`
	BoundTo       string   `json:"bound-to,omitempty"` // empty for detached
	CreatedBy     string   `json:"created-by,omitempty"`
	PinnedRoles   []string `json:"pinned-roles,omitempty"` // service tokens only
	TokenType     string   `json:"token-type,omitempty"`
	CreatedAt     string   `json:"created-at,omitempty"`
	RotatedAt     string   `json:"rotated-at,omitempty"`
	LastUsedAt    string   `json:"last-used-at,omitempty"`
	ExpiresAt     string   `json:"expires-at,omitempty"`
	LifespanHours int64    `json:"lifespan-hours,omitempty"`
}

// CreateAPITokenRequest is the input for CreateAPIToken. Kind defaults to
// "interactive" when empty. PinnedRoles is only meaningful for the service
// kinds (ignored for interactive). LifespanHours=0 uses the server default cap.
type CreateAPITokenRequest struct {
	Description   string
	Kind          string
	PinnedRoles   []string
	LifespanHours int64
}

// CreateAPIToken issues a token for the given user. The path user must be the
// caller (or the caller must be an admin). service_detached is admin-only and
// is created unbound regardless of the path user. The returned token's Token
// field holds the raw secret — store it immediately; the API never returns it
// again.
func (c *Client) CreateAPIToken(ctx context.Context, userID string, req CreateAPITokenRequest) (*APIToken, error) {
	// The create request body uses snake_case attribute keys (Pydantic model),
	// unlike the kebab-case keys the response serialises with.
	attrs := map[string]any{}
	if req.Description != "" {
		attrs["description"] = req.Description
	}
	if req.Kind != "" {
		attrs["kind"] = req.Kind
	}
	if req.LifespanHours > 0 {
		attrs["lifespan_hours"] = req.LifespanHours
	}
	if len(req.PinnedRoles) > 0 {
		attrs["pinned_roles"] = req.PinnedRoles
	}
	body, err := MarshalResource("authentication-tokens", attrs, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create api-token: %w", err)
	}
	data, err := c.Post(ctx,
		fmt.Sprintf("/api/terrapod/v1/users/%s/authentication-tokens", url.PathEscape(userID)),
		body)
	if err != nil {
		return nil, err
	}
	return parseAPIToken(data)
}

// ListUserAPITokens lists a user's own tokens (never includes detached tokens).
// The path user must be the caller, or the caller must be an admin.
func (c *Client) ListUserAPITokens(ctx context.Context, userID string) ([]APIToken, error) {
	return c.listAPITokens(ctx,
		fmt.Sprintf("/api/terrapod/v1/users/%s/authentication-tokens", url.PathEscape(userID)))
}

// ListAllAPITokens lists every token across all users (admin only). Pass a kind
// ("interactive", "service_bound", "service_detached") to filter, or "" for all.
func (c *Client) ListAllAPITokens(ctx context.Context, kind string) ([]APIToken, error) {
	path := "/api/terrapod/v1/admin/authentication-tokens"
	if kind != "" {
		path += "?kind=" + url.QueryEscape(kind)
	}
	return c.listAPITokens(ctx, path)
}

// ListExpiringAPITokens returns service tokens nearing expiry, scoped to the
// caller: their own bound service tokens, plus all detached tokens when the
// caller is an admin. Drives the in-app expiry warnings.
func (c *Client) ListExpiringAPITokens(ctx context.Context) ([]APIToken, error) {
	return c.listAPITokens(ctx, "/api/terrapod/v1/authentication-tokens/expiring")
}

// GetAPIToken fetches one token's metadata by id (the raw Token is never present).
func (c *Client) GetAPIToken(ctx context.Context, tokenID string) (*APIToken, error) {
	data, err := c.Get(ctx,
		fmt.Sprintf("/api/terrapod/v1/authentication-tokens/%s", url.PathEscape(tokenID)))
	if err != nil {
		return nil, err
	}
	return parseAPIToken(data)
}

// RotateAPIToken mints a fresh secret for a token and resets its expiry clock.
// The old secret stops working immediately. The returned token's Token field
// holds the new raw secret.
func (c *Client) RotateAPIToken(ctx context.Context, tokenID string) (*APIToken, error) {
	data, err := c.Post(ctx,
		fmt.Sprintf("/api/terrapod/v1/authentication-tokens/%s/actions/rotate", url.PathEscape(tokenID)),
		nil)
	if err != nil {
		return nil, err
	}
	return parseAPIToken(data)
}

// RetagAPIToken changes a token's kind. interactive <-> service_bound is
// owner-or-admin; converting to/from service_detached is admin-only (and
// unbinds / rebinds the token). pinnedRoles sets the new scope for the service
// kinds (pass nil to leave it unchanged for a bound token; it is cleared when
// converting to interactive).
func (c *Client) RetagAPIToken(ctx context.Context, tokenID, kind string, pinnedRoles []string) (*APIToken, error) {
	attrs := map[string]any{"kind": kind}
	if pinnedRoles != nil {
		attrs["pinned_roles"] = pinnedRoles
	}
	body, err := MarshalResource("authentication-tokens", attrs, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal retag api-token: %w", err)
	}
	data, err := c.Patch(ctx,
		fmt.Sprintf("/api/terrapod/v1/authentication-tokens/%s", url.PathEscape(tokenID)),
		body)
	if err != nil {
		return nil, err
	}
	return parseAPIToken(data)
}

// RevokeAPIToken deletes a single token by id.
func (c *Client) RevokeAPIToken(ctx context.Context, tokenID string) error {
	return c.Delete(ctx,
		fmt.Sprintf("/api/terrapod/v1/authentication-tokens/%s", url.PathEscape(tokenID)))
}

// RevokeAllAPITokensForUser revokes every token bound to an identity — the
// urgent-offboarding lever (admin only). Returns the number revoked. Detached
// tokens are unbound and are not affected.
func (c *Client) RevokeAllAPITokensForUser(ctx context.Context, email string) (int64, error) {
	body, err := json.Marshal(map[string]string{"email": email})
	if err != nil {
		return 0, fmt.Errorf("marshal revoke-all: %w", err)
	}
	data, err := c.Post(ctx,
		"/api/terrapod/v1/admin/authentication-tokens/actions/revoke-all", body)
	if err != nil {
		return 0, err
	}
	// Plain (non-JSON:API) response: {"data": {"email": ..., "revoked": N}}.
	var resp struct {
		Data struct {
			Email   string `json:"email"`
			Revoked int64  `json:"revoked"`
		} `json:"data"`
	}
	if err := json.Unmarshal(data, &resp); err != nil {
		return 0, fmt.Errorf("parse revoke-all response: %w", err)
	}
	return resp.Data.Revoked, nil
}

// ── Internal helpers ─────────────────────────────────────────────────

func (c *Client) listAPITokens(ctx context.Context, path string) ([]APIToken, error) {
	data, err := c.Get(ctx, path)
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, err
	}
	out := make([]APIToken, 0, len(resources))
	for i := range resources {
		out = append(out, *apiTokenFromResource(&resources[i]))
	}
	return out, nil
}

func parseAPIToken(body []byte) (*APIToken, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse api-token response: %w", err)
	}
	return apiTokenFromResource(res), nil
}

func apiTokenFromResource(res *Resource) *APIToken {
	return &APIToken{
		ID:            res.ID,
		Token:         GetStringAttr(res, "token"),
		Description:   GetStringAttr(res, "description"),
		Kind:          GetStringAttr(res, "kind"),
		BoundTo:       GetStringAttr(res, "bound-to"),
		CreatedBy:     GetStringAttr(res, "created-by"),
		PinnedRoles:   GetListAttr(res, "pinned-roles"),
		TokenType:     GetStringAttr(res, "token-type"),
		CreatedAt:     GetStringAttr(res, "created-at"),
		RotatedAt:     GetStringAttr(res, "rotated-at"),
		LastUsedAt:    GetStringAttr(res, "last-used-at"),
		ExpiresAt:     GetStringAttr(res, "expires-at"),
		LifespanHours: GetIntAttr(res, "lifespan-hours"),
	}
}
