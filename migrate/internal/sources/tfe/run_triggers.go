package tfe

import (
	"context"
	"fmt"

	"github.com/hashicorp/go-tfe"

	"github.com/mattrobinsonsre/terrapod/migrate/internal/ir"
)

// RunTriggers reads the inbound run triggers for every workspace in the
// slice and translates them to ir.RunTrigger. A run trigger is a
// cross-workspace dependency: when the SOURCE workspace applies, a run is
// queued on the DESTINATION. Listing "inbound" for a destination yields
// the triggers where that workspace is the destination, with the source
// (Sourceable) populated — go-tfe only returns the sourceable include for
// the inbound filter.
//
// Triggers whose source workspace is outside the migration scope are
// still emitted; the writer resolves both refs and reports any trigger it
// can't create because an endpoint wasn't migrated.
func (c *Client) RunTriggers(ctx context.Context, workspaces []ir.Workspace) ([]ir.RunTrigger, error) {
	var triggers []ir.RunTrigger
	for i := range workspaces {
		dest := &workspaces[i]
		if dest.SourceID == "" {
			continue
		}
		page := 1
		for {
			opts := &tfe.RunTriggerListOptions{
				ListOptions:    tfe.ListOptions{PageNumber: page, PageSize: 100},
				RunTriggerType: tfe.RunTriggerInbound,
				Include:        []tfe.RunTriggerIncludeOpt{tfe.RunTriggerSourceable},
			}
			list, err := c.API.RunTriggers.List(ctx, dest.SourceID, opts)
			if err != nil {
				return nil, fmt.Errorf("list run triggers for workspace %s: %w", dest.Name, err)
			}
			for _, rt := range list.Items {
				if tr, ok := runTriggerToIR(rt, dest.SourceID, dest.Name); ok {
					triggers = append(triggers, tr)
				}
			}
			if list.NextPage == 0 || page >= list.TotalPages {
				break
			}
			page++
		}
	}
	return triggers, nil
}

// runTriggerToIR translates one go-tfe RunTrigger (from an inbound list,
// so Sourceable is the source workspace) into an ir.RunTrigger. Returns
// ok=false when the sourceable relationship is missing (e.g. the source
// workspace was deleted) so the caller skips it. Pure — unit-testable.
func runTriggerToIR(rt *tfe.RunTrigger, destSourceID, destName string) (ir.RunTrigger, bool) {
	if rt == nil || rt.Sourceable == nil || rt.Sourceable.ID == "" {
		return ir.RunTrigger{}, false
	}
	return ir.RunTrigger{
		SourceWorkspaceRef:      rt.Sourceable.ID,
		DestinationWorkspaceRef: destSourceID,
		SourceName:              rt.SourceableName,
		DestinationName:         destName,
	}, true
}
