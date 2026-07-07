package tfe

import (
	"context"
	"fmt"

	"github.com/hashicorp/go-tfe"

	"github.com/mattrobinsonsre/terrapod/migrate/internal/ir"
)

// AgentPools lists the organization's agent pools (with their assigned
// workspaces) and translates them to ir.AgentPool. Only the pool's
// identity + workspace assignments migrate — TFE agent tokens are
// write-only and never returned, so the writer creates the pool but no
// token and reports that a fresh join token + redeployed listeners are
// required.
func (c *Client) AgentPools(ctx context.Context) ([]ir.AgentPool, error) {
	var pools []ir.AgentPool
	page := 1
	for {
		list, err := c.API.AgentPools.List(ctx, c.OrgName, &tfe.AgentPoolListOptions{
			ListOptions: tfe.ListOptions{PageNumber: page, PageSize: 100},
			Include:     []tfe.AgentPoolIncludeOpt{tfe.AgentPoolWorkspaces},
		})
		if err != nil {
			return nil, fmt.Errorf("list agent pools: %w", err)
		}
		for _, ap := range list.Items {
			pools = append(pools, agentPoolToIR(ap))
		}
		if list.NextPage == 0 || page >= list.TotalPages {
			break
		}
		page++
	}
	return pools, nil
}

// agentPoolToIR translates one go-tfe AgentPool. Pure — unit-testable.
// The Workspaces relation (populated via the workspaces include) gives
// the source workspace IDs the pool is assigned to.
func agentPoolToIR(ap *tfe.AgentPool) ir.AgentPool {
	out := ir.AgentPool{
		SourceID: ap.ID,
		Name:     ap.Name,
	}
	for _, ws := range ap.Workspaces {
		if ws != nil && ws.ID != "" {
			out.WorkspaceRefs = append(out.WorkspaceRefs, ws.ID)
		}
	}
	return out
}
