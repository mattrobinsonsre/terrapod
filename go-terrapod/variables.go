package terrapod

import (
	"context"
	"fmt"
	"net/url"
)

// Variable is the decoded form of one Terrapod workspace variable.
//
// Sensitive values are returned redacted by the server (empty Value)
// regardless of who reads — callers must store the configured value
// out-of-band when they need to round-trip it through state. The
// provider's resource layer does this; the migration tool's report
// flags every sensitive variable for operator re-entry.
type Variable struct {
	ID          string `json:"id"`
	Key         string `json:"key"`
	Value       string `json:"value,omitempty"`
	Category    string `json:"category"` // "terraform" | "env"
	HCL         bool   `json:"hcl"`
	Sensitive   bool   `json:"sensitive"`
	Description string `json:"description,omitempty"`
	VersionID   string `json:"version-id,omitempty"`
	CreatedAt   string `json:"created-at,omitempty"`
	UpdatedAt   string `json:"updated-at,omitempty"`
}

// CreateVariableRequest is the input shape for Client.CreateVariable.
// Key + Category are required. Value defaults to empty (legal — many
// env vars are flag-shaped). Sensitive + HCL default to false.
type CreateVariableRequest struct {
	Key         string `json:"key"`
	Value       string `json:"value"`
	Category    string `json:"category"` // "terraform" | "env"
	HCL         bool   `json:"hcl,omitempty"`
	Sensitive   bool   `json:"sensitive,omitempty"`
	Description string `json:"description,omitempty"`
}

// UpdateVariableRequest is the partial-update shape. Pointer fields
// distinguish "leave alone" (nil) from "set to false/empty" (&value).
// Unlike workspaces, every variable field is mechanically updateable
// — there are no immutable-after-create fields, so the surface is
// uniform.
//
// The Key field IS updateable on Terrapod (renaming a variable
// preserves its id + history) — pass an empty string when not
// renaming.
type UpdateVariableRequest struct {
	Key         string  `json:"key,omitempty"`
	Value       *string `json:"value,omitempty"`
	Category    string  `json:"category,omitempty"`
	HCL         *bool   `json:"hcl,omitempty"`
	Sensitive   *bool   `json:"sensitive,omitempty"`
	Description *string `json:"description,omitempty"`
}

// CreateVariable creates a variable scoped to a workspace.
//
// Common errors:
//   - *ConflictError when the key already exists for this workspace
//   - *ValidationError on invalid category
//   - *NotFoundError when workspaceID doesn't resolve
func (c *Client) CreateVariable(ctx context.Context, workspaceID string, req CreateVariableRequest) (*Variable, error) {
	attrs := variableCreateAttrs(req)
	body, err := MarshalResource("vars", attrs, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create variable: %w", err)
	}
	data, err := c.Post(ctx, "/api/v2/workspaces/"+url.PathEscape(workspaceID)+"/vars", body)
	if err != nil {
		return nil, err
	}
	return parseVariable(data)
}

// GetVariable looks up a variable by workspace + id. There's no
// /vars/{id} endpoint directly on Terrapod (variables are scoped to
// their workspace); we list the workspace's variables and find the
// matching id. The list cost is bounded by Terrapod's typical
// per-workspace variable count (single-digit to low-tens). Callers
// who already have the full Variable from a previous CreateVariable
// or ListVariables call don't need this.
//
// Returns *NotFoundError when the id isn't present.
func (c *Client) GetVariable(ctx context.Context, workspaceID, id string) (*Variable, error) {
	list, err := c.ListVariables(ctx, workspaceID)
	if err != nil {
		return nil, err
	}
	for i := range list {
		if list[i].ID == id {
			return &list[i], nil
		}
	}
	return nil, &NotFoundError{Resource: "variable", ID: id}
}

// GetVariableByKey looks up a variable by workspace + key (the
// human-typed name, e.g. "AWS_REGION"). Most operator-facing tools
// reason in keys rather than UUIDs; this is the lookup they want.
// Same per-workspace list cost as GetVariable.
//
// Returns *NotFoundError when no variable with the given key exists
// on the workspace.
func (c *Client) GetVariableByKey(ctx context.Context, workspaceID, key string) (*Variable, error) {
	list, err := c.ListVariables(ctx, workspaceID)
	if err != nil {
		return nil, err
	}
	for i := range list {
		if list[i].Key == key {
			return &list[i], nil
		}
	}
	return nil, &NotFoundError{Resource: "variable", ID: key}
}

// ListVariables returns every variable on a workspace. Terrapod
// doesn't paginate this endpoint (variable counts per workspace are
// small); the SDK returns the full set in one call.
func (c *Client) ListVariables(ctx context.Context, workspaceID string) ([]Variable, error) {
	data, err := c.Get(ctx, "/api/v2/workspaces/"+url.PathEscape(workspaceID)+"/vars")
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, err
	}
	out := make([]Variable, 0, len(resources))
	for i := range resources {
		v := variableFromResource(&resources[i])
		out = append(out, *v)
	}
	return out, nil
}

// UpdateVariable patches a variable in place. Pointer fields on the
// UpdateVariableRequest preserve "leave alone" semantics — nil ↦
// omit from the JSON body, &value ↦ set explicitly. Without this, a
// vanilla update that didn't touch the sensitive flag would flip it
// to false.
func (c *Client) UpdateVariable(ctx context.Context, workspaceID, id string, req UpdateVariableRequest) (*Variable, error) {
	attrs := variableUpdateAttrs(req)
	body, err := MarshalResourceWithID(id, "vars", attrs)
	if err != nil {
		return nil, fmt.Errorf("marshal update variable: %w", err)
	}
	data, err := c.Patch(ctx,
		"/api/v2/workspaces/"+url.PathEscape(workspaceID)+"/vars/"+url.PathEscape(id),
		body,
	)
	if err != nil {
		return nil, err
	}
	return parseVariable(data)
}

// DeleteVariable removes a variable.
func (c *Client) DeleteVariable(ctx context.Context, workspaceID, id string) error {
	return c.Delete(ctx,
		"/api/v2/workspaces/"+url.PathEscape(workspaceID)+"/vars/"+url.PathEscape(id),
	)
}

// ── Internal helpers ─────────────────────────────────────────────────

func variableCreateAttrs(req CreateVariableRequest) map[string]any {
	attrs := map[string]any{
		"key":      req.Key,
		"value":    req.Value,
		"category": req.Category,
	}
	if req.HCL {
		attrs["hcl"] = true
	}
	if req.Sensitive {
		attrs["sensitive"] = true
	}
	if req.Description != "" {
		attrs["description"] = req.Description
	}
	return attrs
}

func variableUpdateAttrs(req UpdateVariableRequest) map[string]any {
	attrs := map[string]any{}
	if req.Key != "" {
		attrs["key"] = req.Key
	}
	if req.Value != nil {
		attrs["value"] = *req.Value
	}
	if req.Category != "" {
		attrs["category"] = req.Category
	}
	if req.HCL != nil {
		attrs["hcl"] = *req.HCL
	}
	if req.Sensitive != nil {
		attrs["sensitive"] = *req.Sensitive
	}
	if req.Description != nil {
		attrs["description"] = *req.Description
	}
	return attrs
}

func parseVariable(body []byte) (*Variable, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse variable response: %w", err)
	}
	return variableFromResource(res), nil
}

func variableFromResource(res *Resource) *Variable {
	return &Variable{
		ID:          res.ID,
		Key:         GetStringAttr(res, "key"),
		Value:       GetStringAttr(res, "value"),
		Category:    GetStringAttr(res, "category"),
		HCL:         GetBoolAttr(res, "hcl"),
		Sensitive:   GetBoolAttr(res, "sensitive"),
		Description: GetStringAttr(res, "description"),
		VersionID:   GetStringAttr(res, "version-id"),
		CreatedAt:   GetStringAttr(res, "created-at"),
		UpdatedAt:   GetStringAttr(res, "updated-at"),
	}
}
