package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
)

// StateGraph is a single workspace's resource dependency graph (#765) — one
// node per resource address in the workspace's Terraform state, wired by the
// "depends-on" relationships Terraform records. Derived server-side from a
// chosen StateVersion (the current one by default).
//
// The grouping axis is not baked in — each resource node ships its type / mode
// / module / provider so the consumer can pivot however it likes.
type StateGraph struct {
	Nodes []StateNode    `json:"nodes"`
	Edges []StateEdge    `json:"edges"`
	Meta  StateGraphMeta `json:"meta"`
}

// StateNode is a single resource in the state.
type StateNode struct {
	ID        string `json:"id"`        // resource address, e.g. "module.net.aws_subnet.a"
	Kind      string `json:"kind"`      // always "resource"
	Name      string `json:"name"`      // the resource address (label)
	Type      string `json:"type"`      // e.g. "aws_subnet"
	Mode      string `json:"mode"`      // "managed" | "data"
	Module    string `json:"module"`    // "(root)" or "module.<name>..."
	Provider  string `json:"provider"`  // short provider name, e.g. "aws"
	Instances int    `json:"instances"` // count/for_each instance count (drawn as a nucleus)
	InDeg     int    `json:"indeg"`     // how many resources depend on this one
}

// StateEdge is a directed dependency (source depends on target).
type StateEdge struct {
	Source string `json:"source"`
	Target string `json:"target"`
	Kind   string `json:"kind"` // "depends-on"
}

// StateGraphVersion is an entry in the version picker list.
type StateGraphVersion struct {
	ID        string `json:"id"` // "sv-<uuid>"
	Serial    int    `json:"serial"`
	CreatedAt string `json:"created_at"`
	IsCurrent bool   `json:"is_current"`
}

// StateGraphMeta carries resource counts, truncation info, the picker list, and
// which version was rendered.
type StateGraphMeta struct {
	Counts         map[string]int      `json:"counts"` // "resources" / "edges"
	Truncated      bool                `json:"truncated"`
	TotalResources int                 `json:"total_resources"`
	MaxNodes       int                 `json:"max_nodes"`
	Versions       []StateGraphVersion `json:"versions"`
	StateVersion   *StateGraphVersion  `json:"state_version"` // nil when no state exists
}

// GetStateGraph fetches a workspace's resource dependency graph. Pass an empty
// stateVersionID for the current (highest-serial) version, or "sv-<uuid>" to
// render an older one. Requires state:read on the workspace.
func (c *Client) GetStateGraph(ctx context.Context, workspaceID, stateVersionID string) (*StateGraph, error) {
	path := fmt.Sprintf("/api/terrapod/v1/workspaces/%s/state-graph", url.PathEscape(workspaceID))
	if stateVersionID != "" {
		path += "?state_version=" + url.QueryEscape(stateVersionID)
	}
	data, err := c.Get(ctx, path)
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse state graph response: %w", err)
	}
	g := &StateGraph{}
	if raw, ok := res.Attributes["nodes"]; ok {
		_ = json.Unmarshal(raw, &g.Nodes)
	}
	if raw, ok := res.Attributes["edges"]; ok {
		_ = json.Unmarshal(raw, &g.Edges)
	}
	if raw, ok := res.Attributes["meta"]; ok {
		_ = json.Unmarshal(raw, &g.Meta)
	}
	return g, nil
}
