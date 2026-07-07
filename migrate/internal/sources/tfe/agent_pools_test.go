package tfe

import (
	"testing"

	"github.com/hashicorp/go-tfe"
)

func TestAgentPoolToIR_Translation(t *testing.T) {
	ap := &tfe.AgentPool{
		ID:   "apool-1",
		Name: "aws-prod",
		Workspaces: []*tfe.Workspace{
			{ID: "ws-a"},
			{ID: "ws-b"},
		},
	}
	got := agentPoolToIR(ap)
	if got.SourceID != "apool-1" || got.Name != "aws-prod" {
		t.Errorf("identity: %+v", got)
	}
	want := []string{"ws-a", "ws-b"}
	if len(got.WorkspaceRefs) != len(want) {
		t.Fatalf("workspace refs: got %v want %v", got.WorkspaceRefs, want)
	}
	for i := range want {
		if got.WorkspaceRefs[i] != want[i] {
			t.Errorf("ref[%d]: got %q want %q", i, got.WorkspaceRefs[i], want[i])
		}
	}
}

func TestAgentPoolToIR_SkipsNilAndEmptyWorkspaces(t *testing.T) {
	// A pool with no workspaces (or a relation that came back with a nil /
	// empty-ID entry) migrates with an empty ref list, not a bogus one.
	ap := &tfe.AgentPool{
		ID:   "apool-2",
		Name: "unassigned",
		Workspaces: []*tfe.Workspace{
			nil,
			{ID: ""},
		},
	}
	got := agentPoolToIR(ap)
	if got.Name != "unassigned" {
		t.Errorf("name: %q", got.Name)
	}
	if len(got.WorkspaceRefs) != 0 {
		t.Errorf("expected no workspace refs, got %v", got.WorkspaceRefs)
	}
}
