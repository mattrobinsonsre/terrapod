package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
)

// VariableSet groups variables that apply to multiple workspaces.
// A `global` varset applies to every workspace; otherwise only
// workspaces explicitly assigned via AssignWorkspaceToVarset.
// `priority` varsets override workspace-local variables (the only
// case where varset variables win over workspace variables).
type VariableSet struct {
	ID             string `json:"id"`
	Name           string `json:"name"`
	Description    string `json:"description,omitempty"`
	Global         bool   `json:"global"`
	Priority       bool   `json:"priority"`
	VarCount       int64  `json:"var-count"`
	WorkspaceCount int64  `json:"workspace-count"`
	CreatedAt      string `json:"created-at,omitempty"`
	UpdatedAt      string `json:"updated-at,omitempty"`
}

// CreateVariableSetRequest is the input shape for CreateVariableSet.
type CreateVariableSetRequest struct {
	Name        string
	Description string
	Global      bool
	Priority    bool
}

// UpdateVariableSetRequest is the partial-update shape.
type UpdateVariableSetRequest struct {
	Name        string
	Description *string
	Global      *bool
	Priority    *bool
}

// CreateVariableSet creates a new variable set under the single
// `default` organization.
func (c *Client) CreateVariableSet(ctx context.Context, req CreateVariableSetRequest) (*VariableSet, error) {
	body, err := MarshalResource("varsets", varsetCreateAttrs(req), nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create varset: %w", err)
	}
	data, err := c.Post(ctx, "/api/v2/organizations/default/varsets", body)
	if err != nil {
		return nil, err
	}
	return parseVariableSet(data)
}

// GetVariableSet reads a varset by id.
func (c *Client) GetVariableSet(ctx context.Context, id string) (*VariableSet, error) {
	data, err := c.Get(ctx, "/api/v2/varsets/"+url.PathEscape(id))
	if err != nil {
		return nil, err
	}
	return parseVariableSet(data)
}

// ListVariableSets returns every varset in the default organization.
// Pagination follows the TFE V2 conventions when the count grows;
// the SDK fetches the first page (which is currently the only page —
// the API doesn't paginate varsets yet).
func (c *Client) ListVariableSets(ctx context.Context) ([]VariableSet, error) {
	data, err := c.Get(ctx, "/api/v2/organizations/default/varsets")
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, err
	}
	out := make([]VariableSet, 0, len(resources))
	for i := range resources {
		out = append(out, *variableSetFromResource(&resources[i]))
	}
	return out, nil
}

// UpdateVariableSet patches a varset.
func (c *Client) UpdateVariableSet(ctx context.Context, id string, req UpdateVariableSetRequest) (*VariableSet, error) {
	body, err := MarshalResourceWithID(id, "varsets", varsetUpdateAttrs(req))
	if err != nil {
		return nil, fmt.Errorf("marshal update varset: %w", err)
	}
	data, err := c.Patch(ctx, "/api/v2/varsets/"+url.PathEscape(id), body)
	if err != nil {
		return nil, err
	}
	return parseVariableSet(data)
}

// DeleteVariableSet removes a varset. Workspaces referencing it
// (via assignment) lose the varset binding; varset variables are
// also deleted.
func (c *Client) DeleteVariableSet(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/v2/varsets/"+url.PathEscape(id))
}

// ── Workspace assignment ─────────────────────────────────────────────

// AssignWorkspaceToVarset binds a workspace to a varset so the
// varset's variables apply to runs in that workspace.
func (c *Client) AssignWorkspaceToVarset(ctx context.Context, varsetID, workspaceID string) error {
	body, err := marshalRelData([]relRef{{ID: workspaceID, Type: "workspaces"}})
	if err != nil {
		return fmt.Errorf("marshal assign body: %w", err)
	}
	_, err = c.Post(ctx,
		fmt.Sprintf("/api/v2/varsets/%s/relationships/workspaces", url.PathEscape(varsetID)),
		body)
	return err
}

// UnassignWorkspaceFromVarset removes the binding. Idempotent.
func (c *Client) UnassignWorkspaceFromVarset(ctx context.Context, varsetID, workspaceID string) error {
	body, err := marshalRelData([]relRef{{ID: workspaceID, Type: "workspaces"}})
	if err != nil {
		return fmt.Errorf("marshal unassign body: %w", err)
	}
	return c.DeleteWithBody(ctx,
		fmt.Sprintf("/api/v2/varsets/%s/relationships/workspaces", url.PathEscape(varsetID)),
		body)
}

// IsWorkspaceAssignedToVarset reads the varset and checks whether
// the workspace is in its `workspaces` relationship. Helper for
// Terraform Read paths that need to verify the assignment still
// exists.
func (c *Client) IsWorkspaceAssignedToVarset(ctx context.Context, varsetID, workspaceID string) (bool, error) {
	data, err := c.Get(ctx, "/api/v2/varsets/"+url.PathEscape(varsetID))
	if err != nil {
		return false, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return false, err
	}
	rel, ok := res.Relationships["workspaces"]
	if !ok || len(rel.Data) == 0 || string(rel.Data) == "null" {
		return false, nil
	}
	var items []RelationshipResource
	if err := json.Unmarshal(rel.Data, &items); err != nil {
		return false, nil
	}
	for _, item := range items {
		if item.ID == workspaceID {
			return true, nil
		}
	}
	return false, nil
}

// ── Internal helpers ─────────────────────────────────────────────────

type relRef struct {
	ID   string `json:"id"`
	Type string `json:"type"`
}

func marshalRelData(items []relRef) ([]byte, error) {
	return json.Marshal(map[string]any{"data": items})
}

func varsetCreateAttrs(req CreateVariableSetRequest) map[string]any {
	attrs := map[string]any{
		"name":     req.Name,
		"global":   req.Global,
		"priority": req.Priority,
	}
	if req.Description != "" {
		attrs["description"] = req.Description
	}
	return attrs
}

func varsetUpdateAttrs(req UpdateVariableSetRequest) map[string]any {
	attrs := map[string]any{}
	if req.Name != "" {
		attrs["name"] = req.Name
	}
	if req.Description != nil {
		attrs["description"] = *req.Description
	}
	if req.Global != nil {
		attrs["global"] = *req.Global
	}
	if req.Priority != nil {
		attrs["priority"] = *req.Priority
	}
	return attrs
}

func parseVariableSet(body []byte) (*VariableSet, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse varset response: %w", err)
	}
	return variableSetFromResource(res), nil
}

func variableSetFromResource(res *Resource) *VariableSet {
	return &VariableSet{
		ID:             res.ID,
		Name:           GetStringAttr(res, "name"),
		Description:    GetStringAttr(res, "description"),
		Global:         GetBoolAttr(res, "global"),
		Priority:       GetBoolAttr(res, "priority"),
		VarCount:       GetIntAttr(res, "var-count"),
		WorkspaceCount: GetIntAttr(res, "workspace-count"),
		CreatedAt:      GetStringAttr(res, "created-at"),
		UpdatedAt:      GetStringAttr(res, "updated-at"),
	}
}
