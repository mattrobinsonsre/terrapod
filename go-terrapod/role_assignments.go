package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
)

// RoleAssignment is one (provider, email, role) binding. The "id"
// on the wire is composite — the API doesn't return a single
// identifier; callers compute one client-side if they need it
// (provider-name/email/role-name).
type RoleAssignment struct {
	ProviderName string `json:"provider-name"`
	Email        string `json:"email"`
	RoleName     string `json:"role-name"`
	CreatedAt    string `json:"created-at,omitempty"`
}

// ListRoleAssignments returns every assignment visible to the
// caller. Admin/audit see everything.
func (c *Client) ListRoleAssignments(ctx context.Context) ([]RoleAssignment, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/role-assignments")
	if err != nil {
		return nil, err
	}
	return parseRoleAssignmentList(data)
}

// ListRoleAssignmentsForIdentity is a convenience filter that returns
// just the assignments for the given (provider, email) pair. The
// server does not currently support a query-param filter so the SDK
// fetches all and filters client-side; callers that need to scale
// past O(thousands) of assignments should rely on a future paginated
// or filterable endpoint.
func (c *Client) ListRoleAssignmentsForIdentity(ctx context.Context, providerName, email string) ([]RoleAssignment, error) {
	all, err := c.ListRoleAssignments(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]RoleAssignment, 0)
	for _, a := range all {
		if a.ProviderName == providerName && a.Email == email {
			out = append(out, a)
		}
	}
	return out, nil
}

// SetRolesForIdentity replaces the full set of roles bound to the
// given (provider, email) pair. The server treats this as
// replace-all: any roles previously assigned but missing from the
// `roles` slice are removed; missing roles are added. Pass an empty
// slice to remove all custom assignments (the user still gets
// implicit `everyone`).
func (c *Client) SetRolesForIdentity(ctx context.Context, providerName, email string, roles []string) error {
	if roles == nil {
		roles = []string{}
	}
	body, err := json.Marshal(map[string]any{
		"data": map[string]any{
			"attributes": map[string]any{
				"provider-name": providerName,
				"email":         email,
				"roles":         roles,
			},
		},
	})
	if err != nil {
		return fmt.Errorf("marshal role assignment PUT: %w", err)
	}
	_, err = c.Put(ctx, "/api/terrapod/v1/role-assignments", body)
	return err
}

// AddRoleToIdentity adds a single role to the existing set. Idempotent —
// re-adding an existing role is a no-op. Implemented as a read-modify-
// write on top of SetRolesForIdentity.
func (c *Client) AddRoleToIdentity(ctx context.Context, providerName, email, roleName string) error {
	current, err := c.ListRoleAssignmentsForIdentity(ctx, providerName, email)
	if err != nil {
		return err
	}
	names := make([]string, 0, len(current)+1)
	for _, a := range current {
		if a.RoleName == roleName {
			return nil // already present
		}
		names = append(names, a.RoleName)
	}
	names = append(names, roleName)
	return c.SetRolesForIdentity(ctx, providerName, email, names)
}

// RemoveRoleFromIdentity removes a single (provider, email, role)
// binding. Uses the direct DELETE endpoint — cheaper than the
// read-modify-write SetRoles path.
func (c *Client) RemoveRoleFromIdentity(ctx context.Context, providerName, email, roleName string) error {
	path := fmt.Sprintf("/api/terrapod/v1/role-assignments/%s/%s/%s",
		url.PathEscape(providerName), url.PathEscape(email), url.PathEscape(roleName))
	return c.Delete(ctx, path)
}

// GetRoleAssignment looks up the (provider, email, role) triple.
// Returns nil + *NotFoundError when no such binding exists (consistent
// with every other Get* in this SDK — callers should test with
// errors.As / IsNotFound).
func (c *Client) GetRoleAssignment(ctx context.Context, providerName, email, roleName string) (*RoleAssignment, error) {
	all, err := c.ListRoleAssignmentsForIdentity(ctx, providerName, email)
	if err != nil {
		return nil, err
	}
	for i := range all {
		if all[i].RoleName == roleName {
			return &all[i], nil
		}
	}
	return nil, &NotFoundError{Resource: "role-assignment", ID: providerName + "/" + email + "/" + roleName}
}

// ── Internal helpers ─────────────────────────────────────────────────

func parseRoleAssignmentList(body []byte) ([]RoleAssignment, error) {
	var doc struct {
		Data []struct {
			Type       string          `json:"type"`
			Attributes json.RawMessage `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, fmt.Errorf("parse role-assignments list: %w", err)
	}
	out := make([]RoleAssignment, 0, len(doc.Data))
	for _, item := range doc.Data {
		var a RoleAssignment
		if len(item.Attributes) > 0 {
			if err := json.Unmarshal(item.Attributes, &a); err != nil {
				return nil, fmt.Errorf("parse role-assignment item: %w", err)
			}
		}
		out = append(out, a)
	}
	return out, nil
}
