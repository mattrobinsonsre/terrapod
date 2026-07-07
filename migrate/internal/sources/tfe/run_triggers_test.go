package tfe

import (
	"testing"

	"github.com/hashicorp/go-tfe"
)

func TestRunTriggerToIR_Translation(t *testing.T) {
	rt := &tfe.RunTrigger{
		ID:             "rt-1",
		SourceableName: "networking",
		Sourceable:     &tfe.Workspace{ID: "ws-src"},
	}
	got, ok := runTriggerToIR(rt, "ws-dest", "app")
	if !ok {
		t.Fatal("expected ok=true for a populated trigger")
	}
	if got.SourceWorkspaceRef != "ws-src" || got.DestinationWorkspaceRef != "ws-dest" ||
		got.SourceName != "networking" || got.DestinationName != "app" {
		t.Errorf("translation: %+v", got)
	}
}

func TestRunTriggerToIR_MissingSourceableSkipped(t *testing.T) {
	// A trigger whose sourceable relationship didn't come back (e.g. the
	// source workspace was deleted) is skipped, not emitted with an empty ref.
	if _, ok := runTriggerToIR(&tfe.RunTrigger{ID: "rt-1"}, "ws-dest", "app"); ok {
		t.Error("trigger with nil Sourceable should be skipped")
	}
	if _, ok := runTriggerToIR(&tfe.RunTrigger{ID: "rt-1", Sourceable: &tfe.Workspace{}}, "ws-dest", "app"); ok {
		t.Error("trigger with empty Sourceable.ID should be skipped")
	}
	if _, ok := runTriggerToIR(nil, "ws-dest", "app"); ok {
		t.Error("nil trigger should be skipped")
	}
}
