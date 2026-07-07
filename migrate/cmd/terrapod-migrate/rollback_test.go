package main

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/migrate/internal/framework"
)

// rollbackFakeServer answers the two endpoints rollback touches:
// GET current-state-version (reports a per-workspace serial) and
// DELETE workspace (records the id). currentSerial maps a terrapod id
// to the serial the destination currently reports; absent → 404.
type rollbackFakeServer struct {
	mu                 sync.Mutex
	currentSerial      map[string]int64
	deleted            []string
	deletedVarsets     []string
	deletedRunTriggers []string
}

func newRollbackServer(t *testing.T, currentSerial map[string]int64) (*rollbackFakeServer, *terrapod.Client) {
	t.Helper()
	fs := &rollbackFakeServer{currentSerial: currentSerial}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/current-state-version"):
			// path: /api/v2/workspaces/{id}/current-state-version
			id := strings.TrimPrefix(r.URL.Path, "/api/v2/workspaces/")
			id = strings.TrimSuffix(id, "/current-state-version")
			fs.mu.Lock()
			serial, ok := fs.currentSerial[id]
			fs.mu.Unlock()
			if !ok {
				http.Error(w, "no state", http.StatusNotFound)
				return
			}
			w.WriteHeader(http.StatusOK)
			_, _ = fmt.Fprintf(w, `{"data":{"id":"sv-x","type":"state-versions","attributes":{"serial":%d,"lineage":"lin","state-size":10}}}`, serial)
		case r.Method == http.MethodDelete && strings.Contains(r.URL.Path, "/api/terrapod/v1/workspaces/"):
			id := strings.TrimPrefix(r.URL.Path, "/api/terrapod/v1/workspaces/")
			fs.mu.Lock()
			fs.deleted = append(fs.deleted, id)
			fs.mu.Unlock()
			w.WriteHeader(http.StatusNoContent)
		case r.Method == http.MethodDelete && strings.Contains(r.URL.Path, "/api/v2/varsets/"):
			id := strings.TrimPrefix(r.URL.Path, "/api/v2/varsets/")
			fs.mu.Lock()
			fs.deletedVarsets = append(fs.deletedVarsets, id)
			fs.mu.Unlock()
			w.WriteHeader(http.StatusNoContent)
		case r.Method == http.MethodDelete && strings.Contains(r.URL.Path, "/api/terrapod/v1/run-triggers/"):
			id := strings.TrimPrefix(r.URL.Path, "/api/terrapod/v1/run-triggers/")
			fs.mu.Lock()
			fs.deletedRunTriggers = append(fs.deletedRunTriggers, id)
			fs.mu.Unlock()
			w.WriteHeader(http.StatusNoContent)
		default:
			http.Error(w, "unhandled "+r.Method+" "+r.URL.Path, http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	c, err := terrapod.NewClient(terrapod.Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return fs, c
}

func TestRollback_Apply_DeletesMigrationCreatedVarsets_SkipsReused(t *testing.T) {
	fs, c := newRollbackServer(t, map[string]int64{})
	state := &framework.State{
		VariableSets: []framework.VariableSetRecord{
			{SourceID: "vs-1", Name: "global-tags", TerrapodID: "varset-a", State: "created", CreatedByMigration: true},
			{SourceID: "vs-2", Name: "reused-set", TerrapodID: "varset-b", State: "created", CreatedByMigration: false},
		},
	}
	report := runRollback(t.Context(), c, state, "", true /*apply*/, false)
	if len(fs.deletedVarsets) != 1 || fs.deletedVarsets[0] != "varset-a" {
		t.Fatalf("expected only varset-a deleted, got %v", fs.deletedVarsets)
	}
	if report.VarsetDeleted != 1 {
		t.Fatalf("VarsetDeleted=%d", report.VarsetDeleted)
	}
	// Migration-created record marked rolled_back + id cleared.
	if state.VariableSets[0].State != "rolled_back" || state.VariableSets[0].TerrapodID != "" {
		t.Fatalf("varset record not marked rolled_back: %+v", state.VariableSets[0])
	}
	// Reused varset (not created by us) is never touched.
	if state.VariableSets[1].State != "created" || state.VariableSets[1].TerrapodID != "varset-b" {
		t.Fatalf("reused varset was modified: %+v", state.VariableSets[1])
	}
	// Idempotent: a re-run deletes nothing more.
	fs.deletedVarsets = nil
	report2 := runRollback(t.Context(), c, state, "", true, false)
	if len(fs.deletedVarsets) != 0 || report2.VarsetDeleted != 0 {
		t.Fatalf("second rollback not idempotent: deleted=%v count=%d", fs.deletedVarsets, report2.VarsetDeleted)
	}
}

func TestRollback_Apply_DeletesMigrationCreatedRunTriggers_SkipsReused(t *testing.T) {
	fs, c := newRollbackServer(t, map[string]int64{})
	state := &framework.State{
		RunTriggers: []framework.RunTriggerRecord{
			{SourceWorkspaceRef: "ws-a", DestinationWorkspaceRef: "ws-b", TerrapodID: "rt-a", State: "created", CreatedByMigration: true},
			// A reused/pre-existing trigger (no id, not created by us) — never deleted.
			{SourceWorkspaceRef: "ws-c", DestinationWorkspaceRef: "ws-b", State: "reused", CreatedByMigration: false},
		},
	}
	report := runRollback(t.Context(), c, state, "", true /*apply*/, false)
	if len(fs.deletedRunTriggers) != 1 || fs.deletedRunTriggers[0] != "rt-a" {
		t.Fatalf("expected only rt-a deleted, got %v", fs.deletedRunTriggers)
	}
	if report.RunTriggerDeleted != 1 {
		t.Fatalf("RunTriggerDeleted=%d", report.RunTriggerDeleted)
	}
	if state.RunTriggers[0].State != "rolled_back" || state.RunTriggers[0].TerrapodID != "" {
		t.Fatalf("record not marked rolled_back: %+v", state.RunTriggers[0])
	}
	// Idempotent re-run.
	fs.deletedRunTriggers = nil
	report2 := runRollback(t.Context(), c, state, "", true, false)
	if len(fs.deletedRunTriggers) != 0 || report2.RunTriggerDeleted != 0 {
		t.Fatalf("second rollback not idempotent: %v", fs.deletedRunTriggers)
	}
}

func TestRollback_DryRun_ListsVarsetsWithoutDeleting(t *testing.T) {
	fs, c := newRollbackServer(t, map[string]int64{})
	state := &framework.State{
		VariableSets: []framework.VariableSetRecord{
			{SourceID: "vs-1", Name: "global-tags", TerrapodID: "varset-a", State: "created", CreatedByMigration: true},
		},
	}
	report := runRollback(t.Context(), c, state, "", false /*dry-run*/, false)
	if len(fs.deletedVarsets) != 0 {
		t.Fatalf("dry-run deleted varsets: %v", fs.deletedVarsets)
	}
	if report.VarsetDeleted != 1 || len(report.VariableSets) != 1 || report.VariableSets[0].Action != "would_delete" {
		t.Fatalf("unexpected dry-run varset report: %+v", report.VariableSets)
	}
}

func TestRollback_DryRun_ListsOnlyCreatedByMigration_NoDeletes(t *testing.T) {
	fs, c := newRollbackServer(t, map[string]int64{"ws-a": 3, "ws-b": 1})
	state := &framework.State{
		Workspaces: []framework.WorkspaceRecord{
			{SourceName: "a", TerrapodID: "ws-a", State: "created", CreatedByMigration: true, StateSerial: 3},
			{SourceName: "b-reused", TerrapodID: "ws-b", State: "created", CreatedByMigration: false, StateSerial: 1},
		},
	}
	report := runRollback(t.Context(), c, state, "", false /*apply*/, false /*force*/)
	if len(fs.deleted) != 0 {
		t.Fatalf("dry-run deleted workspaces: %v", fs.deleted)
	}
	if report.DeletedCount != 1 {
		t.Fatalf("expected 1 would-delete, got %d (%+v)", report.DeletedCount, report.Workspaces)
	}
	if report.Workspaces[0].SourceName != "a" || report.Workspaces[0].Action != "would_delete" {
		t.Fatalf("unexpected target: %+v", report.Workspaces)
	}
}

func TestRollback_Apply_DeletesCreatedOnly_SkipsReused(t *testing.T) {
	fs, c := newRollbackServer(t, map[string]int64{"ws-a": 3, "ws-b": 1})
	state := &framework.State{
		Workspaces: []framework.WorkspaceRecord{
			{SourceName: "a", TerrapodID: "ws-a", State: "created", CreatedByMigration: true, StateSerial: 3},
			{SourceName: "b-reused", TerrapodID: "ws-b", State: "created", CreatedByMigration: false, StateSerial: 1},
		},
	}
	report := runRollback(t.Context(), c, state, "", true, false)
	if len(fs.deleted) != 1 || fs.deleted[0] != "ws-a" {
		t.Fatalf("expected only ws-a deleted, got %v", fs.deleted)
	}
	if report.DeletedCount != 1 {
		t.Fatalf("DeletedCount=%d", report.DeletedCount)
	}
	// The record must be marked rolled_back + id cleared so re-runs skip it.
	if state.Workspaces[0].State != "rolled_back" || state.Workspaces[0].TerrapodID != "" {
		t.Fatalf("record not marked rolled_back: %+v", state.Workspaces[0])
	}
	// Re-running must delete nothing more (idempotent).
	fs.deleted = nil
	report2 := runRollback(t.Context(), c, state, "", true, false)
	if len(fs.deleted) != 0 || report2.DeletedCount != 0 {
		t.Fatalf("second rollback was not idempotent: deleted=%v count=%d", fs.deleted, report2.DeletedCount)
	}
}

func TestRollback_Apply_SkipsAdvancedState_UnlessForce(t *testing.T) {
	// Destination has advanced to serial 7; migration recorded 3.
	fs, c := newRollbackServer(t, map[string]int64{"ws-a": 7})
	state := &framework.State{
		Workspaces: []framework.WorkspaceRecord{
			{SourceName: "a", TerrapodID: "ws-a", State: "created", CreatedByMigration: true, StateSerial: 3},
		},
	}
	report := runRollback(t.Context(), c, state, "", true, false /*force*/)
	if len(fs.deleted) != 0 {
		t.Fatalf("advanced workspace was deleted without --force: %v", fs.deleted)
	}
	if report.SkippedCount != 1 || report.Workspaces[0].Action != "skipped_advanced" {
		t.Fatalf("expected skipped_advanced, got %+v", report.Workspaces)
	}

	// With --force it deletes.
	fs2, c2 := newRollbackServer(t, map[string]int64{"ws-a": 7})
	state2 := &framework.State{
		Workspaces: []framework.WorkspaceRecord{
			{SourceName: "a", TerrapodID: "ws-a", State: "created", CreatedByMigration: true, StateSerial: 3},
		},
	}
	report2 := runRollback(t.Context(), c2, state2, "", true, true /*force*/)
	if len(fs2.deleted) != 1 || report2.DeletedCount != 1 {
		t.Fatalf("--force did not delete advanced workspace: deleted=%v", fs2.deleted)
	}
}
