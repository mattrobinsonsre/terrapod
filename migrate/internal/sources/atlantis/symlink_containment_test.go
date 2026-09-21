package atlantis

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// GHSA-wqfx-wf44-cgvj. The lexical guards refuse `../` but `os.ReadFile`
// follows symlinks below them, so a symlink committed to a repository being
// migrated escapes a check that never sees anything out of place. Git stores
// symlink blobs verbatim (absolute targets included) and core.symlinks defaults
// true on Linux and macOS, so both vectors survive a clone.
//
// What reaches Terrapod is another project's terraform state, uploaded as this
// workspace's — state routinely carries plaintext secrets, and a migration host
// is typically a workstation or CI runner holding several repositories.

func mkRepoWithVictim(t *testing.T) (repo, victim string) {
	t.Helper()
	base := t.TempDir()
	repo = filepath.Join(base, "repo")
	victim = filepath.Join(base, "victim")
	for _, d := range []string{repo, victim} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	state := `{"version":4,"lineage":"VICTIM-LINEAGE","serial":42,"resources":[]}`
	if err := os.WriteFile(filepath.Join(victim, "terraform.tfstate"), []byte(state), 0o644); err != nil {
		t.Fatal(err)
	}
	return repo, victim
}

func TestResolveProjectDirRefusesADirectorySymlinkEscape(t *testing.T) {
	repo, victim := mkRepoWithVictim(t)
	if err := os.Symlink(victim, filepath.Join(repo, "escape")); err != nil {
		t.Skipf("symlinks unavailable: %v", err)
	}

	// Lexically spotless: Rel(repo, repo/escape) is "escape".
	got, err := resolveProjectDir(repo, "escape")
	if err == nil {
		t.Fatalf("a directory symlink escaped the repo root: resolved to %q", got)
	}
	if !strings.Contains(err.Error(), "escapes the repo root") {
		t.Errorf("unexpected error text: %v", err)
	}
}

func TestReadLocalStateRefusesAFileSymlinkEscape(t *testing.T) {
	repo, victim := mkRepoWithVictim(t)
	proj := filepath.Join(repo, "proj")
	if err := os.MkdirAll(proj, 0o755); err != nil {
		t.Fatal(err)
	}
	// The easier vector: the DIRECTORY stays inside the repo and `rel` is the
	// literal "terraform.tfstate", so nothing is lexically out of place at all.
	if err := os.Symlink(filepath.Join(victim, "terraform.tfstate"), filepath.Join(proj, "terraform.tfstate")); err != nil {
		t.Skipf("symlinks unavailable: %v", err)
	}

	raw, err := readLocalState(proj, "")
	if err == nil {
		t.Fatalf("a file symlink exfiltrated %d bytes of another project's state", len(raw))
	}
	if strings.Contains(string(raw), "VICTIM-LINEAGE") {
		t.Fatal("victim state was returned to the caller")
	}
}

func TestTheLexicalGuardsStillRefuseTraversal(t *testing.T) {
	// Controls: the `../` half of the threat the code documents must keep
	// working. A symlink fix that broke these would be a regression.
	repo, _ := mkRepoWithVictim(t)
	for _, dir := range []string{"../victim", "../../etc", "a/../../victim", "/etc"} {
		if _, err := resolveProjectDir(repo, dir); err == nil {
			t.Errorf("resolveProjectDir accepted traversal %q", dir)
		}
	}
	for _, p := range []string{"../victim/terraform.tfstate", "/etc/passwd"} {
		if _, err := readLocalState(repo, p); err == nil {
			t.Errorf("readLocalState accepted traversal %q", p)
		}
	}
}

func TestAnHonestProjectStillResolvesAndReads(t *testing.T) {
	// The fix must not break the ordinary case, including the one where a
	// project simply has no state file yet.
	repo, _ := mkRepoWithVictim(t)
	proj := filepath.Join(repo, "proj")
	if err := os.MkdirAll(proj, 0o755); err != nil {
		t.Fatal(err)
	}
	state := `{"version":4,"lineage":"MINE","serial":1,"resources":[]}`
	if err := os.WriteFile(filepath.Join(proj, "terraform.tfstate"), []byte(state), 0o644); err != nil {
		t.Fatal(err)
	}

	resolved, err := resolveProjectDir(repo, "proj")
	if err != nil {
		t.Fatalf("an ordinary project dir was refused: %v", err)
	}
	raw, err := readLocalState(resolved, "")
	if err != nil {
		t.Fatalf("an ordinary state file was refused: %v", err)
	}
	if !strings.Contains(string(raw), "MINE") {
		t.Errorf("wrong state returned: %s", raw)
	}

	// A project with no state file must still report "no state", not a
	// containment error — EvalSymlinks fails on a missing final component.
	empty := filepath.Join(repo, "empty")
	if err := os.MkdirAll(empty, 0o755); err != nil {
		t.Fatal(err)
	}
	if _, err := readLocalState(empty, ""); err == nil {
		t.Error("expected a no-state error")
	} else if strings.Contains(err.Error(), "escapes") {
		t.Errorf("a missing state file was reported as an escape: %v", err)
	}
}
