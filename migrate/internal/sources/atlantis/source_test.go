package atlantis

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// gitInit creates a tmp dir, runs `git init`, sets a fake origin, and
// makes one commit. Returns the absolute path. Subtests use this to
// avoid mocking git — we just create real (tiny) repos. The migrate
// tool will always be run against a real clone in practice, so
// integration tests that exercise the real git binary catch problems
// our hand-rolled `runGit` stubs would miss.
func gitInit(t *testing.T, remote string, withDefaultBranchSet bool) string {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not available")
	}
	dir := t.TempDir()
	mustRun := func(args ...string) {
		cmd := exec.Command("git", args...)
		cmd.Dir = dir
		cmd.Env = append(os.Environ(),
			"GIT_CONFIG_GLOBAL=/dev/null", // ignore user gitconfig
			"GIT_AUTHOR_NAME=t", "GIT_AUTHOR_EMAIL=t@example.com",
			"GIT_COMMITTER_NAME=t", "GIT_COMMITTER_EMAIL=t@example.com",
		)
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %s: %v\n%s", strings.Join(args, " "), err, string(out))
		}
	}
	mustRun("init", "-q", "-b", "main")
	if remote != "" {
		mustRun("remote", "add", "origin", remote)
	}
	// A commit is needed so symbolic-ref works.
	if err := os.WriteFile(filepath.Join(dir, ".keep"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	mustRun("add", ".keep")
	mustRun("commit", "-q", "-m", "init")
	if withDefaultBranchSet {
		// Simulate what `git clone` does: set origin/HEAD → origin/main
		mustRun("update-ref", "refs/remotes/origin/HEAD", "refs/heads/main")
		mustRun("symbolic-ref", "refs/remotes/origin/HEAD", "refs/heads/main")
	}
	return dir
}

func writeFile(t *testing.T, dir, name, body string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

const minimalYAML = `
version: 3
projects:
  - dir: infra/prod
`

func TestLoadDirectory_Happy(t *testing.T) {
	dir := gitInit(t, "https://github.com/acme/infra", true)
	writeFile(t, dir, "atlantis.yaml", minimalYAML)

	src, err := LoadDirectory(dir, LoadOptions{})
	if err != nil {
		t.Fatalf("LoadDirectory: %v", err)
	}
	if src.RepoURL != "https://github.com/acme/infra" {
		t.Errorf("RepoURL = %q", src.RepoURL)
	}
	if src.DefaultBranch != "main" {
		t.Errorf("DefaultBranch = %q", src.DefaultBranch)
	}
	if src.AtlantisYAML == nil || len(src.AtlantisYAML.Projects) != 1 {
		t.Errorf("AtlantisYAML not populated: %+v", src.AtlantisYAML)
	}
	if !filepath.IsAbs(src.SourcePath) {
		t.Errorf("SourcePath should be absolute, got %q", src.SourcePath)
	}
}

func TestLoadDirectory_SSHRemoteNormalisedToHTTPS(t *testing.T) {
	dir := gitInit(t, "git@github.com:acme/infra.git", true)
	writeFile(t, dir, "atlantis.yaml", minimalYAML)

	src, err := LoadDirectory(dir, LoadOptions{})
	if err != nil {
		t.Fatalf("LoadDirectory: %v", err)
	}
	if src.RepoURL != "https://github.com/acme/infra" {
		t.Errorf("ssh-form not normalised: got %q", src.RepoURL)
	}
}

func TestLoadDirectory_NoOriginYieldsErr(t *testing.T) {
	dir := gitInit(t, "", true)
	writeFile(t, dir, "atlantis.yaml", minimalYAML)

	_, err := LoadDirectory(dir, LoadOptions{})
	if !errors.Is(err, ErrNoGitRemote) {
		t.Errorf("expected ErrNoGitRemote, got: %v", err)
	}
}

func TestLoadDirectory_MissingAtlantisYAMLErrs(t *testing.T) {
	dir := gitInit(t, "https://github.com/acme/infra", true)
	// No atlantis.yaml created.
	_, err := LoadDirectory(dir, LoadOptions{})
	if err == nil {
		t.Fatal("expected error for missing atlantis.yaml")
	}
	if !strings.Contains(err.Error(), "atlantis.yaml") {
		t.Errorf("error should mention atlantis.yaml: %v", err)
	}
}

func TestLoadDirectory_OverridePath(t *testing.T) {
	// Some repos keep atlantis.yaml in .atlantis/atlantis.yaml or similar.
	dir := gitInit(t, "https://github.com/acme/infra", true)
	subdir := filepath.Join(dir, ".atlantis")
	if err := os.Mkdir(subdir, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(subdir, "atlantis.yaml"), []byte(minimalYAML), 0o600); err != nil {
		t.Fatal(err)
	}

	src, err := LoadDirectory(dir, LoadOptions{AtlantisYAMLPath: ".atlantis/atlantis.yaml"})
	if err != nil {
		t.Fatalf("LoadDirectory with override path: %v", err)
	}
	if !strings.HasSuffix(src.AtlantisYAMLPath, "/.atlantis/atlantis.yaml") {
		t.Errorf("AtlantisYAMLPath = %q", src.AtlantisYAMLPath)
	}
}

func TestLoadDirectory_NotADirectory(t *testing.T) {
	// Point --source-dir at a file rather than a directory — clear
	// error rather than a downstream confusing one.
	dir := t.TempDir()
	file := filepath.Join(dir, "notadir.txt")
	if err := os.WriteFile(file, []byte("hi"), 0o600); err != nil {
		t.Fatal(err)
	}
	_, err := LoadDirectory(file, LoadOptions{})
	if err == nil {
		t.Fatal("expected error for non-directory")
	}
	if !strings.Contains(err.Error(), "not a directory") {
		t.Errorf("error should mention non-directory: %v", err)
	}
}

func TestLoadDirectory_MissingOriginHEAD_FallsBackToMain(t *testing.T) {
	// A `git init`'d directory without `git clone` won't have
	// refs/remotes/origin/HEAD set. Default branch derivation
	// should soft-fail and fall back to "main".
	dir := gitInit(t, "https://github.com/acme/infra", false)
	writeFile(t, dir, "atlantis.yaml", minimalYAML)

	src, err := LoadDirectory(dir, LoadOptions{})
	if err != nil {
		t.Fatalf("LoadDirectory: %v", err)
	}
	if src.DefaultBranch != "main" {
		t.Errorf("DefaultBranch fallback = %q, want %q", src.DefaultBranch, "main")
	}
}

// ── normaliseRepoURL table ─────────────────────────────────────────

func TestNormaliseRepoURL(t *testing.T) {
	cases := []struct{ in, want string }{
		// HTTPS forms — straight through, .git stripped
		{"https://github.com/acme/infra", "https://github.com/acme/infra"},
		{"https://github.com/acme/infra.git", "https://github.com/acme/infra"},
		// SSH "git@host:path" form — converted
		{"git@github.com:acme/infra.git", "https://github.com/acme/infra"},
		{"git@github.com:acme/infra", "https://github.com/acme/infra"},
		// ssh:// form — converted
		{"ssh://git@github.com/acme/infra.git", "https://github.com/acme/infra"},
		{"ssh://github.com/acme/infra", "https://github.com/acme/infra"},
		// git:// — bare server convention
		{"git://github.com/acme/infra.git", "https://github.com/acme/infra"},
		// HTTP — left alone (operator may be on a self-hosted plain-HTTP gitlab)
		{"http://gitlab.example/acme/infra", "http://gitlab.example/acme/infra"},
		// Whitespace tolerated (git config sometimes emits trailing newline)
		{"  https://github.com/acme/infra  ", "https://github.com/acme/infra"},
		// Self-hosted gitlab + nested groups via SSH
		{"git@gitlab.example:group/subgroup/repo.git", "https://gitlab.example/group/subgroup/repo"},
		// Unknown shapes returned as-is so operators see them and can correct
		{"file:///tmp/repo", "file:///tmp/repo"},
	}
	for _, c := range cases {
		if got := normaliseRepoURL(c.in); got != c.want {
			t.Errorf("normaliseRepoURL(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}
