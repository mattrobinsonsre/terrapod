package terrapod

import (
	"context"
	"fmt"
	"net/url"
)

// VariableSetVariable is a variable that lives inside a varset.
// Mirrors the workspace-scoped Variable type but exists under
// /api/v2/varsets/{id}/relationships/vars. Sensitive variables have
// Value redacted on the wire.
type VariableSetVariable struct {
	ID          string `json:"id"`
	Key         string `json:"key"`
	Value       string `json:"value,omitempty"`
	Category    string `json:"category"`
	HCL         bool   `json:"hcl"`
	Sensitive   bool   `json:"sensitive"`
	Description string `json:"description,omitempty"`
	VersionID   string `json:"version-id,omitempty"`
	CreatedAt   string `json:"created-at,omitempty"`
	UpdatedAt   string `json:"updated-at,omitempty"`
}

// CreateVarsetVariableRequest is the input shape for adding a
// variable to a varset.
type CreateVarsetVariableRequest struct {
	Key         string
	Value       string
	Category    string
	HCL         bool
	Sensitive   bool
	Description string
}

// UpdateVarsetVariableRequest is the partial-update shape.
type UpdateVarsetVariableRequest struct {
	Key         string
	Value       *string
	Category    string
	HCL         *bool
	Sensitive   *bool
	Description *string
}

// CreateVarsetVariable adds a variable to a varset.
func (c *Client) CreateVarsetVariable(ctx context.Context, varsetID string, req CreateVarsetVariableRequest) (*VariableSetVariable, error) {
	body, err := MarshalResource("vars", varsetVarCreateAttrs(req), nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create varset-var: %w", err)
	}
	data, err := c.Post(ctx,
		fmt.Sprintf("/api/v2/varsets/%s/relationships/vars", url.PathEscape(varsetID)),
		body)
	if err != nil {
		return nil, err
	}
	return parseVarsetVariable(data)
}

// GetVarsetVariable fetches one variable from a varset. The TFE V2
// shape doesn't expose a per-id GET — we list and filter client-side.
// Returns nil + *NotFoundError when no variable matches (consistent
// with every other Get* in this SDK).
func (c *Client) GetVarsetVariable(ctx context.Context, varsetID, varID string) (*VariableSetVariable, error) {
	vars, err := c.ListVarsetVariables(ctx, varsetID)
	if err != nil {
		return nil, err
	}
	for i := range vars {
		if vars[i].ID == varID {
			return &vars[i], nil
		}
	}
	return nil, &NotFoundError{Resource: "varset-variable", ID: varID}
}

// ListVarsetVariables returns every variable in the varset.
func (c *Client) ListVarsetVariables(ctx context.Context, varsetID string) ([]VariableSetVariable, error) {
	data, err := c.Get(ctx,
		fmt.Sprintf("/api/v2/varsets/%s/relationships/vars", url.PathEscape(varsetID)))
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, err
	}
	out := make([]VariableSetVariable, 0, len(resources))
	for i := range resources {
		out = append(out, *varsetVarFromResource(&resources[i]))
	}
	return out, nil
}

// UpdateVarsetVariable patches a varset variable.
func (c *Client) UpdateVarsetVariable(ctx context.Context, varsetID, varID string, req UpdateVarsetVariableRequest) (*VariableSetVariable, error) {
	body, err := MarshalResourceWithID(varID, "vars", varsetVarUpdateAttrs(req))
	if err != nil {
		return nil, fmt.Errorf("marshal update varset-var: %w", err)
	}
	data, err := c.Patch(ctx,
		fmt.Sprintf("/api/v2/varsets/%s/relationships/vars/%s",
			url.PathEscape(varsetID), url.PathEscape(varID)),
		body)
	if err != nil {
		return nil, err
	}
	return parseVarsetVariable(data)
}

// DeleteVarsetVariable removes a variable from a varset.
func (c *Client) DeleteVarsetVariable(ctx context.Context, varsetID, varID string) error {
	return c.Delete(ctx,
		fmt.Sprintf("/api/v2/varsets/%s/relationships/vars/%s",
			url.PathEscape(varsetID), url.PathEscape(varID)))
}

// ── Internal helpers ─────────────────────────────────────────────────

func varsetVarCreateAttrs(req CreateVarsetVariableRequest) map[string]any {
	attrs := map[string]any{
		"key":      req.Key,
		"category": req.Category,
	}
	if req.Value != "" {
		attrs["value"] = req.Value
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

func varsetVarUpdateAttrs(req UpdateVarsetVariableRequest) map[string]any {
	attrs := map[string]any{}
	if req.Key != "" {
		attrs["key"] = req.Key
	}
	if req.Category != "" {
		attrs["category"] = req.Category
	}
	if req.Value != nil {
		attrs["value"] = *req.Value
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

func parseVarsetVariable(body []byte) (*VariableSetVariable, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse varset-var response: %w", err)
	}
	return varsetVarFromResource(res), nil
}

func varsetVarFromResource(res *Resource) *VariableSetVariable {
	return &VariableSetVariable{
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
