package framework

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func TestLoad_MissingFileIsNotAnError(t *testing.T) {
	// First `apply` run has no state file — operator shouldn't see an
	// error about that; just a normal "we're starting fresh" load.
	dir := t.TempDir()
	got, err := Load(filepath.Join(dir, "nope.json"))
	if err != nil {
		t.Fatalf("expected nil error for missing file, got: %v", err)
	}
	if got != nil {
		t.Errorf("expected nil State for missing file, got: %+v", got)
	}
}

func TestRoundTrip_PreservesEverything(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "migration-state.json")
	in := &State{
		Source:     "tfe",
		SourceHost: "app.terraform.io",
		SourceOrg:  "acme",
		DestHost:   "terrapod.acme.example",
		Workspaces: []WorkspaceRecord{
			{
				SourceID:     "ws-aaaa",
				SourceName:   "api-prod",
				TerrapodID:   "ws-terra-1111",
				State:        "created",
				StateLineage: "abcd-1234",
				StateSerial:  42,
				Labels:       map[string]string{"env": "prod", "team": "platform"},
			},
		},
		VCSConnections: []VCSConnectionRecord{
			{SourceID: "oc-1", Name: "github-prod", Provider: "github", TerrapodID: "vcs-x", State: "created"},
		},
		SkippedItems: []SkippedRecord{
			{Kind: "sentinel-policy", Name: "no-public-buckets", Reason: "Terrapod uses OPA"},
		},
	}

	if err := in.Save(path, "0.27.0"); err != nil {
		t.Fatalf("Save: %v", err)
	}
	out, err := Load(path)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if out == nil {
		t.Fatal("Load returned nil after a successful Save")
	}
	// Spot-check rather than DeepEqual — timestamps are set by Save
	// and won't match the zero-valued input.
	if out.Source != "tfe" || out.SourceHost != "app.terraform.io" || out.SourceOrg != "acme" || out.DestHost != "terrapod.acme.example" {
		t.Errorf("top-level fields not preserved: %+v", out)
	}
	if len(out.Workspaces) != 1 || out.Workspaces[0].SourceID != "ws-aaaa" || out.Workspaces[0].StateSerial != 42 {
		t.Errorf("workspace not preserved: %+v", out.Workspaces)
	}
	if got, want := out.Workspaces[0].Labels["env"], "prod"; got != want {
		t.Errorf("label env: got %q want %q", got, want)
	}
	if len(out.VCSConnections) != 1 || out.VCSConnections[0].Provider != "github" {
		t.Errorf("vcs connection not preserved: %+v", out.VCSConnections)
	}
	if len(out.SkippedItems) != 1 || out.SkippedItems[0].Kind != "sentinel-policy" {
		t.Errorf("skipped item not preserved: %+v", out.SkippedItems)
	}
}

func TestSave_StampsHousekeepingFields(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "s.json")
	s := &State{Source: "atlantis"}

	before := time.Now().UTC()
	if err := s.Save(path, "0.27.0"); err != nil {
		t.Fatalf("Save: %v", err)
	}
	after := time.Now().UTC()

	if s.Version != SchemaVersion {
		t.Errorf("Save should stamp Version=%d, got %d", SchemaVersion, s.Version)
	}
	if s.ToolVersion != "0.27.0" {
		t.Errorf("Save should stamp ToolVersion, got %q", s.ToolVersion)
	}
	if s.CreatedAt.Before(before) || s.CreatedAt.After(after) {
		t.Errorf("CreatedAt should be ~now, got %v", s.CreatedAt)
	}
	if s.UpdatedAt.Before(before) || s.UpdatedAt.After(after) {
		t.Errorf("UpdatedAt should be ~now, got %v", s.UpdatedAt)
	}
}

func TestSave_CreatedAtPreservedAcrossSaves(t *testing.T) {
	// CreatedAt is the timestamp of the FIRST `apply` — re-running on
	// a partial migration must not bump it. UpdatedAt is the latest
	// save; that does bump.
	dir := t.TempDir()
	path := filepath.Join(dir, "s.json")
	s := &State{Source: "tfe"}

	if err := s.Save(path, "0.27.0"); err != nil {
		t.Fatalf("Save: %v", err)
	}
	firstCreated := s.CreatedAt
	firstUpdated := s.UpdatedAt

	time.Sleep(10 * time.Millisecond) // ensure UpdatedAt can differ
	if err := s.Save(path, "0.27.0"); err != nil {
		t.Fatalf("Save again: %v", err)
	}
	if !s.CreatedAt.Equal(firstCreated) {
		t.Errorf("CreatedAt should be preserved, got %v -> %v", firstCreated, s.CreatedAt)
	}
	if !s.UpdatedAt.After(firstUpdated) {
		t.Errorf("UpdatedAt should advance, got %v -> %v", firstUpdated, s.UpdatedAt)
	}
}

func TestLoad_FutureSchemaIsRejected(t *testing.T) {
	// An older binary reading a newer file would silently zero out
	// fields it doesn't recognise on the next Save. That's a recipe
	// for losing migration progress. Refuse.
	dir := t.TempDir()
	path := filepath.Join(dir, "s.json")
	if err := os.WriteFile(path, []byte(`{"version": 999, "source": "tfe"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	_, err := Load(path)
	if !errors.Is(err, ErrFutureSchema) {
		t.Errorf("expected ErrFutureSchema, got: %v", err)
	}
}

func TestLoad_UnknownSchemaIsRejected(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "s.json")
	if err := os.WriteFile(path, []byte(`{"version": 0, "source": "tfe"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	_, err := Load(path)
	if !errors.Is(err, ErrUnknownSchema) {
		t.Errorf("expected ErrUnknownSchema, got: %v", err)
	}
}

func TestLoad_CorruptJSONErrors(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "s.json")
	if err := os.WriteFile(path, []byte(`{not json}`), 0o600); err != nil {
		t.Fatal(err)
	}
	_, err := Load(path)
	if err == nil {
		t.Fatal("expected an error on corrupt JSON")
	}
	if !strings.Contains(err.Error(), "parse") {
		t.Errorf("error message should mention parsing, got: %v", err)
	}
}

func TestSave_AtomicMeansNoPartialFileOnFailure(t *testing.T) {
	// Hard to fault-inject Save's writes directly, but the public
	// guarantee is "no temp file is left around on success" — verify
	// that, and the rename-to-target invariant by inspecting the dir.
	dir := t.TempDir()
	path := filepath.Join(dir, "s.json")
	s := &State{Source: "tfe"}
	if err := s.Save(path, "0.27.0"); err != nil {
		t.Fatalf("Save: %v", err)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 {
		var names []string
		for _, e := range entries {
			names = append(names, e.Name())
		}
		t.Errorf("Save left extra files behind: %v", names)
	}
}

func TestSave_FilePermsAre0600(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Windows POSIX perm bits don't carry through Chmod the same way")
	}
	dir := t.TempDir()
	path := filepath.Join(dir, "s.json")
	s := &State{Source: "tfe"}
	if err := s.Save(path, "0.27.0"); err != nil {
		t.Fatalf("Save: %v", err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Errorf("expected 0600, got %o", perm)
	}
}

func TestSave_OutputIsHumanReadable(t *testing.T) {
	// Operators open this file. The format must be indented and
	// terminated with a newline — the trailing newline avoids
	// `git diff` noise and the indent makes review possible.
	dir := t.TempDir()
	path := filepath.Join(dir, "s.json")
	s := &State{Source: "tfe", SourceHost: "app.terraform.io"}
	if err := s.Save(path, "0.27.0"); err != nil {
		t.Fatalf("Save: %v", err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasSuffix(string(data), "\n") {
		t.Error("output should end with a newline")
	}
	if !strings.Contains(string(data), "  \"source\":") {
		t.Errorf("output should be 2-space indented, got:\n%s", string(data))
	}
	// Sanity: the file is still valid JSON.
	var into map[string]any
	if err := json.Unmarshal(data, &into); err != nil {
		t.Errorf("output is not valid JSON: %v", err)
	}
}

func TestWorkspaceLookups(t *testing.T) {
	s := &State{
		Workspaces: []WorkspaceRecord{
			{SourceID: "ws-a", SourceName: "api-prod"},
			{SourceID: "ws-b", SourceName: "api-staging"},
		},
	}
	if got := s.WorkspaceBySourceID("ws-a"); got == nil || got.SourceName != "api-prod" {
		t.Errorf("WorkspaceBySourceID(ws-a) = %+v", got)
	}
	if got := s.WorkspaceBySourceID("missing"); got != nil {
		t.Errorf("WorkspaceBySourceID(missing) should be nil, got %+v", got)
	}
	if got := s.WorkspaceBySourceName("api-staging"); got == nil || got.SourceID != "ws-b" {
		t.Errorf("WorkspaceBySourceName(api-staging) = %+v", got)
	}
	if got := s.WorkspaceBySourceName("missing"); got != nil {
		t.Errorf("WorkspaceBySourceName(missing) should be nil, got %+v", got)
	}
}

func TestDefaultStateFile_IsExpected(t *testing.T) {
	// docs/migration.md references this exact filename and `apply` uses
	// it as the --state-file default. Locking it as a test prevents an
	// accidental rename from drifting the docs / the rewriter UX.
	if DefaultStateFile != "migration-state.json" {
		t.Errorf("DefaultStateFile changed: %q — update docs/migration.md and the rewriter UX if intentional", DefaultStateFile)
	}
}
