package terrapod

import (
	"encoding/json"
	"strings"
)

// Document is the JSON:API top-level wrapper. Every Terrapod response
// (single resource or list) is shaped as one of these. Data, Included,
// and Meta are kept as RawMessage so consumers can decode into
// resource-specific structs without a second deserialise pass.
type Document struct {
	Data     json.RawMessage `json:"data"`
	Included json.RawMessage `json:"included,omitempty"`
	Meta     json.RawMessage `json:"meta,omitempty"`
}

// Resource is one JSON:API resource object: id + type + attributes
// (decoded as RawMessage per-key so callers Get<Type>Attr them at
// read time) + optional relationships.
type Resource struct {
	ID            string                     `json:"id"`
	Type          string                     `json:"type"`
	Attributes    map[string]json.RawMessage `json:"attributes"`
	Relationships map[string]Relationship    `json:"relationships,omitempty"`
}

// Relationship is one to-one or to-many relationship slot.
type Relationship struct {
	Data json.RawMessage `json:"data"`
}

// RelationshipResource is the id+type pair inside a to-one
// relationship's Data.
type RelationshipResource struct {
	ID   string `json:"id"`
	Type string `json:"type"`
}

// ErrorResponse is the top-level shape of a JSON:API error body.
// Terrapod populates Errors on every 4xx + 5xx where the handler ran;
// transport errors (timeouts, refused connections) never produce one.
type ErrorResponse struct {
	Errors []ErrorObject `json:"errors"`
}

// ErrorObject is one JSON:API error. Status is the string form of
// the HTTP status (e.g. "422"); Title is the short category
// ("Unprocessable Entity"); Detail is the per-incident message
// — surface that one to operators.
type ErrorObject struct {
	Status string `json:"status"`
	Title  string `json:"title"`
	Detail string `json:"detail"`
}

// MarshalResource builds a JSON:API create-request body. Attributes
// are required; relationships are optional. Callers shape both as
// map[string]any with the JSON value the API expects for each key.
//
// Returns marshalled bytes ready to pass to Client.Post.
func MarshalResource(resourceType string, attributes map[string]any, relationships map[string]any) ([]byte, error) {
	data := map[string]any{
		"type":       resourceType,
		"attributes": attributes,
	}
	if relationships != nil {
		data["relationships"] = relationships
	}
	return json.Marshal(map[string]any{"data": data})
}

// MarshalResourceWithID builds an update-request body that carries
// the resource's id (some PATCH endpoints require it in the body
// even when it's in the URL path; safer to always include).
func MarshalResourceWithID(id, resourceType string, attributes map[string]any) ([]byte, error) {
	return MarshalResourceWithIDAndRels(id, resourceType, attributes, nil)
}

// MarshalResourceWithIDAndRels is like MarshalResourceWithID but also
// includes relationships in the body. Use when an update needs to
// reattach (e.g. moving a variable to a different variable set).
func MarshalResourceWithIDAndRels(id, resourceType string, attributes, relationships map[string]any) ([]byte, error) {
	data := map[string]any{
		"id":         id,
		"type":       resourceType,
		"attributes": attributes,
	}
	if relationships != nil {
		data["relationships"] = relationships
	}
	return json.Marshal(map[string]any{"data": data})
}

// ParseResource decodes a JSON:API single-resource response into a
// *Resource. Returns nil + error on malformed input.
func ParseResource(body []byte) (*Resource, error) {
	var doc Document
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, err
	}
	var res Resource
	if err := json.Unmarshal(doc.Data, &res); err != nil {
		return nil, err
	}
	return &res, nil
}

// ParseResourceList decodes a JSON:API list response into a slice of
// Resource values. The order of the slice matches the order on the
// wire (Terrapod's list endpoints are typically pagination-ordered).
func ParseResourceList(body []byte) ([]Resource, error) {
	var doc Document
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, err
	}
	var resources []Resource
	if err := json.Unmarshal(doc.Data, &resources); err != nil {
		return nil, err
	}
	return resources, nil
}

// GetStringAttr returns a string-typed attribute from r, defaulting
// to empty when the key is absent or the value isn't a string.
// Errors are swallowed on purpose — the common-case caller doesn't
// want to handle them, and a missing-or-malformed attribute reads as
// zero-value consistently.
func GetStringAttr(r *Resource, key string) string {
	raw, ok := r.Attributes[key]
	if !ok || len(raw) == 0 {
		return ""
	}
	var s string
	if err := json.Unmarshal(raw, &s); err != nil {
		return ""
	}
	return s
}

// GetBoolAttr returns a bool-typed attribute from r.
func GetBoolAttr(r *Resource, key string) bool {
	raw, ok := r.Attributes[key]
	if !ok || len(raw) == 0 {
		return false
	}
	var b bool
	if err := json.Unmarshal(raw, &b); err != nil {
		return false
	}
	return b
}

// GetIntAttr returns an int64-typed attribute from r. Floats from
// the wire (Terrapod's API sometimes returns ints as JSON numbers
// without fractional parts) are coerced to int64.
func GetIntAttr(r *Resource, key string) int64 {
	raw, ok := r.Attributes[key]
	if !ok || len(raw) == 0 {
		return 0
	}
	var n json.Number
	if err := json.Unmarshal(raw, &n); err != nil {
		return 0
	}
	i, err := n.Int64()
	if err != nil {
		return 0
	}
	return i
}

// GetFloat64Attr returns a float64-typed attribute from r.
func GetFloat64Attr(r *Resource, key string) float64 {
	raw, ok := r.Attributes[key]
	if !ok || len(raw) == 0 {
		return 0
	}
	var f float64
	if err := json.Unmarshal(raw, &f); err != nil {
		return 0
	}
	return f
}

// GetMapAttr returns a map[string]string-typed attribute from r,
// returning nil when the value is absent OR JSON null. Useful for
// Terrapod's `labels` attribute on most resources.
func GetMapAttr(r *Resource, key string) map[string]string {
	raw, ok := r.Attributes[key]
	if !ok || len(raw) == 0 || string(raw) == "null" {
		return nil
	}
	var m map[string]string
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil
	}
	return m
}

// GetListAttr returns a []string-typed attribute from r, nil-safe.
func GetListAttr(r *Resource, key string) []string {
	raw, ok := r.Attributes[key]
	if !ok || len(raw) == 0 || string(raw) == "null" {
		return nil
	}
	var l []string
	if err := json.Unmarshal(raw, &l); err != nil {
		return nil
	}
	return l
}

// GetRelationshipID returns the id of a to-one relationship by name,
// or "" when the relationship is missing / null / unparsable.
func GetRelationshipID(r *Resource, name string) string {
	rel, ok := r.Relationships[name]
	if !ok || len(rel.Data) == 0 || string(rel.Data) == "null" {
		return ""
	}
	var rr RelationshipResource
	if err := json.Unmarshal(rel.Data, &rr); err != nil {
		return ""
	}
	return rr.ID
}

// ListMeta is the pagination shape Terrapod returns in the `meta`
// block of a JSON:API list response. Every list endpoint emits this
// shape; SDK list methods decode it via parseListMeta and surface the
// useful fields on their typed list-result struct (Workspaces.List
// returns WorkspaceList with CurrentPage/TotalPages/TotalCount).
type ListMeta struct {
	CurrentPage int `json:"current-page"`
	TotalPages  int `json:"total-pages"`
	TotalCount  int `json:"total-count"`
}

// parseListMeta extracts pagination info from a JSON:API list
// response. Missing or unparsable meta returns zero-valued ListMeta
// with no error — callers proceed without pagination info rather than
// fail the whole list.
func parseListMeta(body []byte) (ListMeta, error) {
	var doc struct {
		Meta struct {
			Pagination ListMeta `json:"pagination"`
		} `json:"meta"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		return ListMeta{}, err
	}
	return doc.Meta.Pagination, nil
}

// StripPrefix removes a typed-id prefix (e.g. "ws-abc123" → "abc123").
// Inverse of AddPrefix. Safe to call when the prefix is already absent.
func StripPrefix(id, prefix string) string {
	return strings.TrimPrefix(id, prefix)
}

// AddPrefix prepends a typed-id prefix if not already present.
// Idempotent.
func AddPrefix(id, prefix string) string {
	if strings.HasPrefix(id, prefix) {
		return id
	}
	return prefix + id
}
