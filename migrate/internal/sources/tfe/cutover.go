package tfe

import (
	"context"
	"errors"
	"fmt"

	"github.com/hashicorp/go-tfe"
)

// LockWorkspaces locks every TFE workspace listed in workspaceIDs to
// prevent the source platform from running new applies during the
// cutover window. Returns the count locked and any per-workspace
// errors (one entry per failure; the function does not abort on a
// single workspace's failure — operators want the full picture).
//
// The lock reason is stamped into go-tfe's LockOptions.Reason so
// anyone looking at the source-side workspace can see it's locked
// because of a migration and not assume something's stuck. Locking
// is idempotent on TFE — a lock attempt against an already-locked
// workspace returns the lock state unchanged.
func (c *Client) LockWorkspaces(ctx context.Context, workspaceIDs []string, reason string) (locked int, errs []error) {
	if reason == "" {
		reason = "Locked by terrapod-migrate during cutover; see terrapod-migrate handover doc"
	}
	for _, id := range workspaceIDs {
		_, err := c.API.Workspaces.Lock(ctx, id, tfe.WorkspaceLockOptions{Reason: &reason})
		if err != nil {
			// Already-locked: TFE returns 409 with a message we can
			// safely treat as success. Other errors bubble up.
			if errors.Is(err, tfe.ErrWorkspaceLocked) {
				locked++
				continue
			}
			errs = append(errs, fmt.Errorf("lock workspace %s: %w", id, err))
			continue
		}
		locked++
	}
	return locked, errs
}

// UnlockWorkspaces is the inverse — runs when the operator decides
// to roll back a cutover and resume on the source side. Same error
// semantics: per-workspace failures are returned individually.
func (c *Client) UnlockWorkspaces(ctx context.Context, workspaceIDs []string) (unlocked int, errs []error) {
	for _, id := range workspaceIDs {
		_, err := c.API.Workspaces.Unlock(ctx, id)
		if err != nil {
			if errors.Is(err, tfe.ErrWorkspaceNotLocked) {
				unlocked++
				continue
			}
			errs = append(errs, fmt.Errorf("unlock workspace %s: %w", id, err))
			continue
		}
		unlocked++
	}
	return unlocked, errs
}
