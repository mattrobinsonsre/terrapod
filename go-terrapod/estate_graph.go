package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
)

// EstateGraph is the whole-estate topology graph (#763) — every workspace the
// caller can see plus the registry modules they use, wired by the
// cross-workspace structure Terrapod holds centrally. Derived server-side and
// RBAC-filtered to visible workspaces.
//
// The grouping axis is deliberately NOT baked in — each workspace node ships its
// raw labels / pool / name so the consumer can pivot however it likes (the
// platform enforces no labelling convention).
type EstateGraph struct {
	Nodes []EstateNode    `json:"nodes"`
	Edges []EstateEdge    `json:"edges"`
	Meta  EstateGraphMeta `json:"meta"`
}

// EstateNode is a workspace or a module.
type EstateNode struct {
	ID     string            `json:"id"`     // "ws-<uuid>" or "mod-<uuid>"
	Kind   string            `json:"kind"`   // "workspace" | "module"
	Name   string            `json:"name"`   // workspace name, or "name/provider" for a module
	Labels map[string]string `json:"labels"` // workspace labels (empty for modules)
	Pool   string            `json:"pool"`   // agent pool name, or "(local)"/"(no pool)"
	InDeg  int               `json:"indeg"`  // how many things depend on this node
}

// EstateEdge is a directed dependency between two nodes.
type EstateEdge struct {
	Source string `json:"source"`
	Target string `json:"target"`
	Kind   string `json:"kind"` // "remote-state" | "run-trigger" | "uses-module"
}

// EstateGraphMeta carries estate-wide counts.
type EstateGraphMeta struct {
	Counts map[string]int `json:"counts"` // keyed by "workspaces"/"modules"/"edges"
}

// GetEstateGraph fetches the whole-estate topology graph visible to the caller.
// The result is filtered to the workspaces the caller can read, so a token with
// narrow RBAC sees a correspondingly smaller graph.
func (c *Client) GetEstateGraph(ctx context.Context) (*EstateGraph, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/estate-graph")
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse estate graph response: %w", err)
	}
	g := &EstateGraph{}
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
