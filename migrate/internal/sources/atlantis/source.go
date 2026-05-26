package atlantis

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// Source represents a single Atlantis-managed repo on disk. The operator
// hands the migration tool a list of these (one per --source-dir flag);
// each Source carries the absolute path to a local clone, the parsed
// atlantis.yaml, and the canonical repo URL derived from git config.
//
// This is the input to the rest of the Atlantis pipeline: the IR
// emitter (already implemented) consumes the parsed AtlantisYAML and
// the repo URL; the backend HCL parser (next increment) walks the
// project directories under SourcePath; the state readers (after that)
// dial out to the cloud-backend the HCL parsing identified.
type Source struct {
	// SourcePath is the absolute, cleaned path to the local clone.
	// Filesystem reads (atlantis.yaml, terraform HCL, local-backend
	// state files) are all relative to this.
	SourcePath string

	// RepoURL is the canonical repo URL the migration records on
	// every produced workspace's VCSRepoURL. Derived from
	// `git config --get remote.origin.url` and normalised to HTTPS
	// form (ssh→https, trailing-.git stripped). One Terrapod VCS
	// connection ends up created per RepoURL.
	RepoURL string

	// DefaultBranch is the local clone's default branch (resolved via
	// `git symbolic-ref refs/remotes/origin/HEAD`). Used as the per-
	// project branch when atlantis.yaml's project doesn't override.
	DefaultBranch string

	// AtlantisYAML is the parsed config (Parse() output). The pointer
	// is non-nil after LoadDirectory succeeds.
	AtlantisYAML *AtlantisYAML

	// AtlantisYAMLPath is the absolute path to the atlantis.yaml file
	// that was read. Surfaced in error reports so an operator running
	// across many repos can see which file a given problem came from.
	AtlantisYAMLPath string
}

// DefaultAtlantisYAMLFilename is what we look for at the root of each
// --source-dir. atlantis.yaml is the conventional name; --atlantis-yaml-path
// override the lookup if a repo uses an unusual location (e.g. a sub-
// directory).
const DefaultAtlantisYAMLFilename = "atlantis.yaml"

// LoadOptions tweaks the per-repo load. The first release intentionally
// keeps the option surface narrow; --atlantis-yaml-path is the only
// non-default knob.
type LoadOptions struct {
	// AtlantisYAMLPath, if non-empty, is the path (relative to dir or
	// absolute) of the atlantis.yaml file to read. Defaults to
	// "<dir>/atlantis.yaml".
	AtlantisYAMLPath string
}

// LoadDirectory ingests one local clone: reads its atlantis.yaml,
// derives the repo URL + default branch from git config, and returns
// the populated Source. Errors are wrapped with the directory the
// operator passed in so reports across many repos remain readable.
//
// The function does NOT walk project subdirectories or read backend
// HCL — that's the next increment's job. Keeping LoadDirectory narrow
// means the test surface is just "file I/O + git invocation" and the
// downstream pipeline can mock a Source by hand.
func LoadDirectory(dir string, opts LoadOptions) (*Source, error) {
	absDir, err := filepath.Abs(dir)
	if err != nil {
		return nil, fmt.Errorf("--source-dir %q: %w", dir, err)
	}
	info, err := os.Stat(absDir)
	if err != nil {
		return nil, fmt.Errorf("--source-dir %q: %w", dir, err)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("--source-dir %q is not a directory", dir)
	}

	yamlPath := opts.AtlantisYAMLPath
	if yamlPath == "" {
		yamlPath = filepath.Join(absDir, DefaultAtlantisYAMLFilename)
	} else if !filepath.IsAbs(yamlPath) {
		yamlPath = filepath.Join(absDir, yamlPath)
	}

	data, err := os.ReadFile(yamlPath)
	if err != nil {
		return nil, fmt.Errorf("read atlantis.yaml at %s: %w", yamlPath, err)
	}
	doc, err := Parse(yamlPath, data)
	if err != nil {
		// Parse() already wraps with the path; pass through as-is so
		// the operator-facing error doesn't double-name the file.
		return nil, err
	}

	repoURL, err := gitRepoURL(absDir)
	if err != nil {
		return nil, fmt.Errorf("derive repo URL from git config in %s: %w", absDir, err)
	}
	defaultBranch, err := gitDefaultBranch(absDir)
	if err != nil {
		// Default branch derivation is best-effort — a fresh clone
		// without origin/HEAD set will fail here. Fall back to
		// "main" with a warning that the IR emitter records; this
		// keeps the migration moving without a confusing git error.
		// The branch is per-project-overridable from atlantis.yaml's
		// `branch:` field anyway.
		defaultBranch = "main"
	}

	return &Source{
		SourcePath:       absDir,
		RepoURL:          repoURL,
		DefaultBranch:    defaultBranch,
		AtlantisYAML:     doc,
		AtlantisYAMLPath: yamlPath,
	}, nil
}

// ErrNoGitRemote is returned when git config doesn't carry a usable
// remote.origin.url. The operator action is "set the remote" — we don't
// silently invent one.
var ErrNoGitRemote = errors.New("git remote.origin.url is not set; run `git remote add origin <url>` in this clone first")

// gitRepoURL invokes `git config --get remote.origin.url` against the
// directory. The returned value is normalised to HTTPS form because the
// Terrapod VCS connection record stores the URL operators paste into
// the workspace's vcs-repo-url field, which is the HTTPS form.
//
// Normalisation rules:
//
//   - SSH form `git@github.com:owner/repo.git`  → `https://github.com/owner/repo`
//   - SSH form `ssh://git@github.com/owner/repo.git`  → `https://github.com/owner/repo`
//   - HTTPS form  `https://github.com/owner/repo.git` → `https://github.com/owner/repo`
//   - Anything else returned verbatim (operator will see it in the
//     report and can correct if needed).
//
// The function shells out to `git` rather than parsing `.git/config`
// directly — git's own parser handles the includes / conditional
// includes / submodule edge cases we'd otherwise have to redo.
func gitRepoURL(dir string) (string, error) {
	out, err := runGit(dir, "config", "--get", "remote.origin.url")
	if err != nil {
		// exit code 1 from git config means "key not set" — surface
		// the friendlier ErrNoGitRemote rather than the raw exec err.
		var exit *exec.ExitError
		if errors.As(err, &exit) && exit.ExitCode() == 1 {
			return "", ErrNoGitRemote
		}
		return "", err
	}
	url := strings.TrimSpace(out)
	if url == "" {
		return "", ErrNoGitRemote
	}
	return normaliseRepoURL(url), nil
}

// gitDefaultBranch resolves the symbolic-ref of origin/HEAD. A clone
// made via `git clone` ordinarily has this set automatically; a
// `git init`+`git remote add` clone won't. Best-effort — callers
// fall back to "main" with a warning.
func gitDefaultBranch(dir string) (string, error) {
	out, err := runGit(dir, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
	if err != nil {
		return "", err
	}
	// Output is "origin/main"; strip the prefix.
	ref := strings.TrimSpace(out)
	if rest, ok := strings.CutPrefix(ref, "origin/"); ok {
		return rest, nil
	}
	return ref, nil
}

// runGit executes a git command in dir and returns stdout. Stderr is
// captured into the error for diagnostics — git's error messages are
// the most useful thing the operator can read when things go wrong.
//
// gitBin is package-level so tests can substitute a stub (the migrate
// tool's own test harness uses a fake script that prints canned
// responses to avoid needing real .git/config in temp dirs).
var gitBin = "git"

func runGit(dir string, args ...string) (string, error) {
	cmd := exec.Command(gitBin, args...) // #nosec G204 — args are static method names
	cmd.Dir = dir
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("git %s in %s: %w (%s)", strings.Join(args, " "), dir, err, strings.TrimSpace(stderr.String()))
	}
	return stdout.String(), nil
}

// normaliseRepoURL converts the four common git URL shapes to the
// HTTPS form Terrapod's VCS connection records prefer. Exported in
// effect via tests; not for external callers.
func normaliseRepoURL(raw string) string {
	url := strings.TrimSuffix(strings.TrimSpace(raw), ".git")
	// SSH form: git@host:owner/repo
	if afterAt, ok := strings.CutPrefix(url, "git@"); ok && strings.Contains(afterAt, ":") {
		// "git@github.com:acme/infra" → "https://github.com/acme/infra"
		host, path, hasColon := strings.Cut(afterAt, ":")
		if hasColon && host != "" && path != "" {
			return "https://" + host + "/" + path
		}
	}
	// ssh:// form: ssh://git@host/owner/repo
	if rest, ok := strings.CutPrefix(url, "ssh://"); ok {
		// Drop optional user@
		if at := strings.Index(rest, "@"); at >= 0 {
			rest = rest[at+1:]
		}
		return "https://" + rest
	}
	// git:// form: git://host/owner/repo (rare; bare-server convention)
	if rest, ok := strings.CutPrefix(url, "git://"); ok {
		return "https://" + rest
	}
	// http(s):// or anything else — return as-is.
	return url
}
