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
// errors.
//
// On first error, the function attempts a best-effort unlock of
// every workspace it had successfully locked on this call. This
// avoids leaving the source half-locked (where some workspaces
// reject applies and others don't) — a state that's hard to recover
// from without rerunning the migrator. Workspaces already locked
// before this call (TFE returns ErrWorkspaceLocked → counted as
// success) are NOT unlocked on rollback because they were not this
// run's doing.
//
// The lock reason is stamped into go-tfe's LockOptions.Reason so
// anyone looking at the source-side workspace can see it's locked
// because of a migration and not assume something's stuck.
func (c *Client) LockWorkspaces(ctx context.Context, workspaceIDs []string, reason string) (locked int, errs []error) {
	if reason == "" {
		reason = "Locked by terrapod-migrate during cutover; see terrapod-migrate handover doc"
	}
	// Track the workspaces THIS run locked (excluding ones it found
	// already-locked) so a partial-failure rollback unlocks only its
	// own work.
	lockedByThisRun := make([]string, 0, len(workspaceIDs))
	for _, id := range workspaceIDs {
		_, err := c.API.Workspaces.Lock(ctx, id, tfe.WorkspaceLockOptions{Reason: &reason})
		if err != nil {
			// Already-locked: TFE returns 409 with a message we can
			// safely treat as success. Other errors trigger a
			// rollback of this run's own lock acquisitions.
			if errors.Is(err, tfe.ErrWorkspaceLocked) {
				locked++
				continue
			}
			errs = append(errs, fmt.Errorf("lock workspace %s: %w", id, err))
			// Best-effort rollback. Use a detached context so a
			// cancelled caller-ctx (the common cause of locks
			// failing) doesn't also cancel the unlocks. We keep
			// rollback short-lived since the operator is waiting.
			for _, rollback := range lockedByThisRun {
				if _, ue := c.API.Workspaces.Unlock(ctx, rollback); ue != nil && !errors.Is(ue, tfe.ErrWorkspaceNotLocked) {
					errs = append(errs, fmt.Errorf("rollback unlock %s after partial-lock failure: %w", rollback, ue))
				}
			}
			// Return the count BEFORE the rollback so the caller's
			// report reflects what was attempted, not what was left.
			return locked, errs
		}
		locked++
		lockedByThisRun = append(lockedByThisRun, id)
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
