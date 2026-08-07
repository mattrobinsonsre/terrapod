package main

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	"github.com/mattrobinsonsre/terrapod/migrate/internal/sources/atlantis"
)

// TestLoadAtlantisPlan_FromFakeClone exercises the apply-subcommand's
// loadAtlantisPlan helper against a temporary directory that mimics a
// real local clone (atlantis.yaml + git remote URL). The test only
// covers the "happy path" — Atlantis fixture parsing has its own
// dedicated tests in internal/sources/atlantis.
func TestLoadAtlantisPlan_FromFakeClone(t *testing.T) {
	// Skip cleanly when git isn't on PATH (CI runners that don't
	// install git would error out and obscure real test failures).
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git binary not available; skipping atlantis-clone fixture")
	}

	dir := t.TempDir()
	yaml := `version: 3
projects:
  - name: app
    dir: app
    branch: /main/
`
	if err := os.WriteFile(filepath.Join(dir, "atlantis.yaml"), []byte(yaml), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(dir, "app"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "app", "main.tf"), []byte("# nothing\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	// Initialise a git repo with a remote so LoadDirectory can derive
	// RepoURL. The test repo is throwaway — no actual git server.
	runGit := func(args ...string) {
		cmd := exec.Command("git", args...)
		cmd.Dir = dir
		cmd.Env = append(os.Environ(),
			"GIT_AUTHOR_NAME=t", "GIT_AUTHOR_EMAIL=t@t",
			"GIT_COMMITTER_NAME=t", "GIT_COMMITTER_EMAIL=t@t",
		)
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v (%s)", args, err, out)
		}
	}
	runGit("init", "-b", "main")
	runGit("remote", "add", "origin", "https://github.com/acme/infra")
	runGit("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

	plan, _, err := loadAtlantisPlan(dir, "", atlantis.StateOptions{})
	if err != nil {
		t.Fatalf("loadAtlantisPlan: %v", err)
	}
	if plan.Source != "atlantis" {
		t.Errorf("plan.Source = %q", plan.Source)
	}
	if len(plan.VCSConnections) != 1 {
		t.Errorf("expected 1 vcs connection, got %d", len(plan.VCSConnections))
	}
	if plan.VCSConnections[0].Provider != "github" {
		t.Errorf("provider: %q", plan.VCSConnections[0].Provider)
	}
	if len(plan.Workspaces) < 1 {
		t.Errorf("expected ≥1 workspace, got %d", len(plan.Workspaces))
	}
	// All workspaces should carry the connection ref so the writer
	// can resolve vcs_connection_id against the existing Terrapod
	// connections at apply time.
	connSourceID := plan.VCSConnections[0].SourceID
	for _, ws := range plan.Workspaces {
		if ws.VCSConnectionRef != connSourceID {
			t.Errorf("workspace %q vcs ref = %q, want %q", ws.Name, ws.VCSConnectionRef, connSourceID)
		}
	}
}

func TestLoadDirectWorkspacePlan_Shape(t *testing.T) {
	plan, stateReader, err := loadDirectWorkspacePlan("/tmp/fake", "my-workspace", atlantis.StateOptions{})
	if err != nil {
		t.Fatalf("loadDirectWorkspacePlan: %v", err)
	}
	if plan.Source != "atlantis" {
		t.Errorf("plan.Source = %q, want %q", plan.Source, "atlantis")
	}
	if plan.SourceMetadata["mode"] != "direct-workspace" {
		t.Errorf("metadata mode = %q", plan.SourceMetadata["mode"])
	}
	if plan.SourceMetadata["workspace"] != "my-workspace" {
		t.Errorf("metadata workspace = %q", plan.SourceMetadata["workspace"])
	}
	if len(plan.Workspaces) != 1 {
		t.Fatalf("expected 1 workspace, got %d", len(plan.Workspaces))
	}
	ws := plan.Workspaces[0]
	if ws.SourceID != "direct:my-workspace" {
		t.Errorf("SourceID = %q, want %q", ws.SourceID, "direct:my-workspace")
	}
	if ws.Name != "my-workspace" {
		t.Errorf("Name = %q", ws.Name)
	}
	if stateReader == nil {
		t.Fatal("stateReader is nil")
	}
}

func TestLoadDirectWorkspacePlan_StateReaderCallsThrough(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "main.tf"), []byte("terraform {}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	state := `{"version":4,"lineage":"direct-test-abc","serial":42,"terraform_version":"1.12.0","outputs":{},"resources":[]}`
	if err := os.WriteFile(filepath.Join(dir, "terraform.tfstate"), []byte(state), 0o644); err != nil {
		t.Fatal(err)
	}

	_, stateReader, err := loadDirectWorkspacePlan(dir, "ws", atlantis.StateOptions{})
	if err != nil {
		t.Fatalf("loadDirectWorkspacePlan: %v", err)
	}

	raw, lineage, serial, err := stateReader(context.Background(), "ignored-source-id")
	if err != nil {
		t.Fatalf("stateReader: %v", err)
	}
	if string(raw) != state {
		t.Errorf("raw mismatch")
	}
	if lineage != "direct-test-abc" {
		t.Errorf("lineage = %q", lineage)
	}
	if serial != 42 {
		t.Errorf("serial = %d", serial)
	}
}

func TestHostFromRepoURL(t *testing.T) {
	// hostFromRepoURL is a best-effort host extractor used for label
	// names and provider auto-detection — it intentionally splits on
	// the first `:` or `/` so SSH-style `git@host:org/repo` URLs
	// collapse to the bare hostname.
	cases := map[string]string{
		"https://github.com/acme/infra":     "github.com",
		"http://github.com/acme/infra":      "github.com",
		"git@github.com:acme/infra.git":     "github.com",
		"https://gitlab.example.com/g/repo": "gitlab.example.com",
	}
	for in, want := range cases {
		if got := hostFromRepoURL(in); got != want {
			t.Errorf("hostFromRepoURL(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestProviderFromRepoURL(t *testing.T) {
	cases := map[string]string{
		"https://github.com/o/r":    "github",
		"https://gitlab.com/g/r":    "gitlab",
		"https://gitlab.acme.com/r": "gitlab", // self-hosted gitlab detected via "gitlab." prefix
	}
	for in, want := range cases {
		if got := providerFromRepoURL(in); got != want {
			t.Errorf("providerFromRepoURL(%q) = %q, want %q", in, got, want)
		}
	}
}
