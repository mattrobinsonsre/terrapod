package framework

import (
	"bytes"
	"strings"
	"testing"
	"time"
)

func TestRenderHandoverMarkdown_HappyPath(t *testing.T) {
	state := &State{
		Version:     1,
		UpdatedAt:   time.Date(2026, 5, 26, 14, 30, 0, 0, time.UTC),
		ToolVersion: "v0.27.0",
		Source:      "tfe",
		SourceHost:  "app.terraform.io",
		SourceOrg:   "acme",
		DestHost:    "terrapod.example.com",
		Workspaces: []WorkspaceRecord{
			{SourceName: "app", TerrapodID: "ws-aaa", State: "created", StateSerial: 7, StateLineage: "abcdef1234567890"},
			{SourceName: "api", TerrapodID: "ws-bbb", State: "created", StateSerial: 3, StateLineage: "zzzzzzzz"},
		},
		VCSConnections: []VCSConnectionRecord{
			{Name: "tfe-github", Provider: "github", TerrapodID: "vcs-1", State: "created"},
		},
		SkippedItems: []SkippedRecord{
			{Kind: "sentinel-policy", Name: "prod-only-no-public-buckets", Reason: "Sentinel is not supported by Terrapod"},
			{Kind: "team", Name: "engineering", Reason: "Terrapod uses label-based RBAC; map this to a role"},
		},
	}
	out := string(RenderHandoverMarkdown(state))

	for _, want := range []string{
		"# Terrapod Migration Handover",
		"**Source platform:** `tfe`",
		"**Source host:** `app.terraform.io`",
		"**Source org:** `acme`",
		"**Destination:** `terrapod.example.com`",
		"## Workspaces (2)",
		"`app`",
		"`ws-aaa`",
		"`api`",
		"`ws-bbb`",
		"## VCS Connections (1)",
		"## Skipped — Manual Action Required (2)",
		"### sentinel-policy",
		"### team",
		"## Cutover Checklist",
		"terrapod-migrate cutover --lock",
		"terrapod-migrate rewrite --dir",
		"terrapod-migrate verify --target",
		"terrapod.example.com",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("output missing %q\nfull output:\n%s", want, out)
		}
	}
}

func TestRenderHandoverMarkdown_Deterministic(t *testing.T) {
	// Same inputs → same bytes. Lets the doc be checked into version
	// control and diffed across runs.
	state := &State{
		Source:    "atlantis",
		SourceHost: "github.com",
		DestHost:  "terrapod.example.com",
		UpdatedAt: time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC),
		Workspaces: []WorkspaceRecord{
			{SourceName: "b-second", TerrapodID: "ws-2", State: "created"},
			{SourceName: "a-first", TerrapodID: "ws-1", State: "created"},
		},
	}
	a := RenderHandoverMarkdown(state)
	b := RenderHandoverMarkdown(state)
	if !bytes.Equal(a, b) {
		t.Errorf("renders are non-deterministic")
	}
	// Workspaces should be sorted alphabetically in the output.
	if idxA, idxB := bytes.Index(a, []byte("a-first")), bytes.Index(a, []byte("b-second")); idxA == -1 || idxB == -1 || idxA > idxB {
		t.Errorf("workspaces not sorted alphabetically: a-first idx=%d, b-second idx=%d", idxA, idxB)
	}
}

func TestRenderHandoverMarkdown_EmptyState(t *testing.T) {
	// Brand-new state file (apply hasn't run yet) — the renderer
	// shouldn't panic and should still produce a usable doc.
	out := string(RenderHandoverMarkdown(&State{}))
	if !strings.Contains(out, "# Terrapod Migration Handover") {
		t.Errorf("missing title in empty-state output: %s", out)
	}
}
