package tfe

import (
	"context"
	"fmt"

	"github.com/hashicorp/go-tfe"

	"github.com/mattrobinsonsre/terrapod/migrate/internal/ir"
)

// tfeToTerrapodTrigger maps the TFE notification trigger vocabulary onto
// Terrapod's. TFE triggers with no Terrapod equivalent
// (assessment:failed, workspace:*, change_request:*) are dropped and
// reported. TFE's assessment:drifted maps to Terrapod's run:drift_detected.
var tfeToTerrapodTrigger = map[string]string{
	"run:created":         "run:created",
	"run:planning":        "run:planning",
	"run:needs_attention": "run:needs_attention",
	"run:applying":        "run:applying",
	"run:completed":       "run:completed",
	"run:errored":         "run:errored",
	"assessment:drifted":  "run:drift_detected",
}

// Notifications reads every migrated workspace's notification
// configurations and translates them to ir.NotificationConfiguration.
// Unsupported destination types (e.g. microsoft-teams) are skipped and
// reported; unsupported triggers are dropped; write-only HMAC tokens and
// TFE user recipients are flagged for operator follow-up.
func (c *Client) Notifications(ctx context.Context, workspaces []ir.Workspace) ([]ir.NotificationConfiguration, []ir.SkippedItem, error) {
	var (
		configs []ir.NotificationConfiguration
		skipped []ir.SkippedItem
	)
	for i := range workspaces {
		ws := &workspaces[i]
		if ws.SourceID == "" {
			continue
		}
		page := 1
		for {
			list, err := c.API.NotificationConfigurations.List(ctx, ws.SourceID, &tfe.NotificationConfigurationListOptions{
				ListOptions: tfe.ListOptions{PageNumber: page, PageSize: 100},
			})
			if err != nil {
				return nil, nil, fmt.Errorf("list notification configs for workspace %s: %w", ws.Name, err)
			}
			for _, nc := range list.Items {
				cfg, sk, ok := notificationToIR(nc, ws.SourceID, ws.Name)
				skipped = append(skipped, sk...)
				if ok {
					configs = append(configs, cfg)
				}
			}
			if list.NextPage == 0 || page >= list.TotalPages {
				break
			}
			page++
		}
	}
	return configs, skipped, nil
}

// notificationToIR translates one go-tfe NotificationConfiguration. Pure
// — unit-testable. Returns ok=false (with a SkippedItem) for destination
// types Terrapod doesn't support.
func notificationToIR(nc *tfe.NotificationConfiguration, wsRef, wsName string) (ir.NotificationConfiguration, []ir.SkippedItem, bool) {
	if nc == nil {
		return ir.NotificationConfiguration{}, nil, false
	}
	dt := string(nc.DestinationType)
	switch dt {
	case "generic", "slack", "email":
	default:
		return ir.NotificationConfiguration{}, []ir.SkippedItem{{
			Kind: "tfe-notification-unsupported-type",
			Name: fmt.Sprintf("workspace %s: %s (%s)", wsName, nc.Name, dt),
			Reason: fmt.Sprintf("Notification destination type %q has no Terrapod equivalent "+
				"(Terrapod supports generic webhook, Slack, and email). Recreate by hand if needed.", dt),
		}}, false
	}

	out := ir.NotificationConfiguration{
		WorkspaceRef:    wsRef,
		Name:            nc.Name,
		DestinationType: dt,
		URL:             nc.URL,
		Enabled:         nc.Enabled,
		EmailAddresses:  append([]string(nil), nc.EmailAddresses...),
		WorkspaceName:   wsName,
	}

	var skipped []ir.SkippedItem

	var dropped []string
	for _, t := range nc.Triggers {
		if mapped, ok := tfeToTerrapodTrigger[t]; ok {
			out.Triggers = append(out.Triggers, mapped)
		} else {
			dropped = append(dropped, t)
		}
	}
	if len(dropped) > 0 {
		skipped = append(skipped, ir.SkippedItem{
			Kind: "tfe-notification-trigger",
			Name: fmt.Sprintf("workspace %s: %s", wsName, nc.Name),
			Reason: fmt.Sprintf("Notification trigger(s) %v have no Terrapod equivalent and were dropped; "+
				"the config migrates with its supported triggers.", dropped),
		})
	}

	// Generic webhooks may sign with an HMAC token TFE never returns
	// (write-only). Migrate with an empty token + flag for re-entry.
	if dt == "generic" {
		out.NeedsToken = true
		skipped = append(skipped, ir.SkippedItem{
			Kind: "tfe-notification-token",
			Name: fmt.Sprintf("workspace %s: %s", wsName, nc.Name),
			Reason: "Generic webhook HMAC tokens are write-only at the source and are never returned. " +
				"The config is migrated with an empty token; re-enter it on the Terrapod notification " +
				"config if the receiver verifies signatures.",
		})
	}

	// TFE user recipients can't be resolved to raw email addresses.
	if len(nc.EmailUsers) > 0 {
		skipped = append(skipped, ir.SkippedItem{
			Kind: "tfe-notification-email-users",
			Name: fmt.Sprintf("workspace %s: %s", wsName, nc.Name),
			Reason: fmt.Sprintf("%d email recipient(s) were TFE user references (not raw addresses) and "+
				"could not be migrated; add their email addresses to the Terrapod notification config.", len(nc.EmailUsers)),
		})
	}

	return out, skipped, true
}
