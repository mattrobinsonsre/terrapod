package client

import (
	"encoding/json"
	"strings"
)

// Document represents a JSON:API top-level document.
type Document struct {
	Data     json.RawMessage `json:"data"`
	Included json.RawMessage `json:"included,omitempty"`
	Meta     json.RawMessage `json:"meta,omitempty"`
}

// Resource represents a single JSON:API resource object.
type Resource struct {
	ID            string                     `json:"id"`
	Type          string                     `json:"type"`
	Attributes    map[string]json.RawMessage `json:"attributes"`
	Relationships map[string]Relationship    `json:"relationships,omitempty"`
}

// Relationship represents a JSON:API relationship.
type Relationship struct {
	Data json.RawMessage `json:"data"`
}

// RelationshipResource is the data inside a relationship.
type RelationshipResource struct {
	ID   string `json:"id"`
	Type string `json:"type"`
}

// ErrorResponse represents a JSON:API error response.
type ErrorResponse struct {
	Errors []ErrorObject `json:"errors"`
}

// ErrorObject is a single JSON:API error.
type ErrorObject struct {
	Status string `json:"status"`
	Title  string `json:"title"`
	Detail string `json:"detail"`
}

// MarshalResource builds a JSON:API request body for create/update.
func MarshalResource(resourceType string, attributes map[string]interface{}, relationships map[string]interface{}) ([]byte, error) {
	body := map[string]interface{}{
		"data": map[string]interface{}{
			"type":       resourceType,
			"attributes": attributes,
		},
	}
	if relationships != nil {
		body["data"].(map[string]interface{})["relationships"] = relationships
	}
	return json.Marshal(body)
}

// MarshalResourceWithID builds a JSON:API request body that includes an ID (for PATCH).
func MarshalResourceWithID(id, resourceType string, attributes map[string]interface{}) ([]byte, error) {
	body := map[string]interface{}{
		"data": map[string]interface{}{
			"id":         id,
			"type":       resourceType,
			"attributes": attributes,
		},
	}
	return json.Marshal(body)
}

// MarshalResourceWithIDAndRels builds a JSON:API request body with ID and relationships (for PATCH).
func MarshalResourceWithIDAndRels(id, resourceType string, attributes map[string]interface{}, relationships map[string]interface{}) ([]byte, error) {
	data := map[string]interface{}{
		"id":         id,
		"type":       resourceType,
		"attributes": attributes,
	}
	if relationships != nil {
		data["relationships"] = relationships
	}
	return json.Marshal(map[string]interface{}{"data": data})
}

// ParseResource extracts a Resource from a JSON:API document.
func ParseResource(data []byte) (*Resource, error) {
	var doc Document
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, err
	}
	var res Resource
	if err := json.Unmarshal(doc.Data, &res); err != nil {
		return nil, err
	}
	return &res, nil
}

// ParseResourceList extracts a list of Resources from a JSON:API document.
func ParseResourceList(data []byte) ([]Resource, error) {
	var doc Document
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, err
	}
	var resources []Resource
	if err := json.Unmarshal(doc.Data, &resources); err != nil {
		return nil, err
	}
	return resources, nil
}

// GetStringAttr reads a string attribute, returning "" if missing.
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

// GetBoolAttr reads a boolean attribute, returning false if missing.
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

// GetIntAttr reads an integer attribute, returning 0 if missing.
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

// GetFloat64Attr reads a float64 attribute.
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

// GetMapAttr reads a map[string]string attribute.
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

// GetListAttr reads a []string attribute.
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

// GetRelationshipID extracts the ID from a to-one relationship.
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

// StripPrefix removes a typed ID prefix (e.g. "ws-" from "ws-abc123").
func StripPrefix(id, prefix string) string {
	return strings.TrimPrefix(id, prefix)
}

// AddPrefix adds a typed ID prefix if not already present.
func AddPrefix(id, prefix string) string {
	if strings.HasPrefix(id, prefix) {
		return id
	}
	return prefix + id
}
