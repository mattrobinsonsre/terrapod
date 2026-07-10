package terrapod

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"strings"
)

// ImpactGraph is the compact plan dependency + blast-radius graph derived
// server-side from a run's stored JSON plan output (#761). Nodes are the
// resources in the plan (coloured by planned action), edges are the
// dependencies between them, reconstructed by walking the plan's
// configuration module tree with cross-module var/output binding.
//
// Powers the run-page "Impact graph" tab. Derived server-side (rather than
// shipping the raw plan JSON to the browser) so the payload stays small and is
// reachable through the BFF in every storage backend.
type ImpactGraph struct {
	// RunID is the bare run UUID (the "run-" prefix is stripped), resolved
	// from the `run` relationship.
	RunID string            `json:"-"`
	Nodes []ImpactGraphNode `json:"nodes"`
	Edges []ImpactGraphEdge `json:"edges"`
	Meta  ImpactGraphMeta   `json:"meta"`
}

// ImpactGraphNode is one resource in the plan.
type ImpactGraphNode struct {
	ID       string  `json:"id"`       // fully-qualified address, e.g. module.vpc.aws_subnet.this["a"]
	Type     string  `json:"type"`     // resource type, e.g. aws_subnet
	Name     string  `json:"name"`     // resource name
	Provider string  `json:"provider"` // short provider name, e.g. aws
	Action   string  `json:"action"`   // create | update | replace | delete | noop
	Key      *string `json:"key"`      // for_each/count key, if any (else null)
	Module   string  `json:"module"`   // module path, e.g. "vpc" or "eks.ng" ("" = root)
}

// ImpactGraphEdge is a dependency: Source depends on Target.
type ImpactGraphEdge struct {
	Source string `json:"source"`
	Target string `json:"target"`
}

// ImpactGraphMeta carries plan-wide metadata.
type ImpactGraphMeta struct {
	TerraformVersion string `json:"terraform_version"`
	// Counts is keyed by action (create/update/replace/delete/noop).
	Counts map[string]int `json:"counts"`
}

// GetRunImpactGraph fetches the impact graph for a run.
//
// runID accepts either a bare run UUID or the prefixed "run-<uuid>" form.
//
// Returns *NotFoundError when the run produced no JSON plan output (the run
// never planned, or ran on an engine/version that didn't emit structured plan
// JSON) — the graph can't be derived without it.
func (c *Client) GetRunImpactGraph(ctx context.Context, runID string) (*ImpactGraph, error) {
	if runID == "" {
		return nil, errors.New("run id is required")
	}
	id := runID
	if len(id) > 4 && id[:4] != "run-" {
		id = "run-" + id
	}
	data, err := c.Get(ctx, "/api/terrapod/v1/runs/"+url.PathEscape(id)+"/impact-graph")
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse impact graph response: %w", err)
	}
	g := &ImpactGraph{RunID: strings.TrimPrefix(GetRelationshipID(res, "run"), "run-")}
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
