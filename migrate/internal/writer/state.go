package writer

import (
	"context"
	"fmt"
)

// StateReader is the writer-side abstraction for "load the latest
// state for this workspace". Sources implement this once per source
// type (TFE pulls via go-tfe, atlantis pulls via the HCL backend
// detection + native S3/local/etc. clients) and hand the writer a
// callback. The writer doesn't care which source-side machinery is
// in play.
//
// Returns (raw bytes, lineage, serial) of the current state version,
// or *ErrNoStateForWorkspace when the source has no state for this
// workspace (which is a normal case for never-applied workspaces —
// the writer skips state migration and logs a Note in the Report).
type StateReader func(ctx context.Context, workspaceSourceID string) ([]byte, string, int64, error)

// ErrNoStateForWorkspace is sentinel-returned from a StateReader when
// the source has no state for the requested workspace. The writer
// treats this as a skip-with-Note, not an error.
type ErrNoStateForWorkspace struct {
	WorkspaceSourceID string
}

// Error matches errors.As / errors.Is semantics so callers can
// distinguish "no state" from real read failures.
func (e *ErrNoStateForWorkspace) Error() string {
	return fmt.Sprintf("source has no state for workspace %q", e.WorkspaceSourceID)
}
