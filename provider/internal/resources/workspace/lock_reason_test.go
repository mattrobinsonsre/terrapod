package workspace

import (
	"context"
	"testing"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// The lock reason and holder (#1705) are read-only and mirror the server:
// populated while a lock is held, null otherwise.
func TestReadWorkspaceIntoModelLockReason(t *testing.T) {
	ctx := context.Background()

	var locked workspaceModel
	ws := &terrapod.Workspace{ID: "ws-a", Name: "a", Locked: true, LockReason: "maintenance window", LockedBy: "ops@example.com"}
	if diags := readWorkspaceIntoModel(ctx, ws, &locked); diags.HasError() {
		t.Fatalf("read: %v", diags)
	}
	if locked.LockReason.ValueString() != "maintenance window" || locked.LockedBy.ValueString() != "ops@example.com" {
		t.Errorf("locked: reason=%v by=%v", locked.LockReason, locked.LockedBy)
	}

	var unlocked workspaceModel
	if diags := readWorkspaceIntoModel(ctx, &terrapod.Workspace{ID: "ws-b", Name: "b"}, &unlocked); diags.HasError() {
		t.Fatalf("read: %v", diags)
	}
	if !unlocked.LockReason.IsNull() || !unlocked.LockedBy.IsNull() {
		t.Errorf("unlocked: reason=%v by=%v", unlocked.LockReason, unlocked.LockedBy)
	}
}
