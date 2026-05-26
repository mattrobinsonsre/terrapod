package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/migrate/internal/framework"
	"github.com/mattrobinsonsre/terrapod/migrate/internal/sources/atlantis"
	"github.com/mattrobinsonsre/terrapod/migrate/internal/writer"
)

// TestE2E_AtlantisToFakeTerrapod is the end-to-end happy-path:
// real atlantis fixture on disk → loadAtlantisPlan → writer.Run with
// a fake Terrapod backend. Confirms the whole `apply` chain hangs
// together without hitting a real Terrapod.
func TestE2E_AtlantisToFakeTerrapod(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git binary not available")
	}

	// ── Atlantis fixture ──────────────────────────────────────────
	cloneDir := t.TempDir()
	atlantisYAML := `version: 3
projects:
  - name: app
    dir: app
    branch: /main/
  - name: api
    dir: api
    branch: /main/
`
	if err := os.WriteFile(filepath.Join(cloneDir, "atlantis.yaml"), []byte(atlantisYAML), 0o644); err != nil {
		t.Fatal(err)
	}
	for _, sub := range []string{"app", "api"} {
		if err := os.MkdirAll(filepath.Join(cloneDir, sub), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(cloneDir, sub, "main.tf"), []byte("# empty\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	runGit := func(args ...string) {
		cmd := exec.Command("git", args...)
		cmd.Dir = cloneDir
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

	// ── Fake Terrapod ─────────────────────────────────────────────
	var (
		mu                sync.Mutex
		workspacesCreated int
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		if r.Body != nil {
			_, _ = io.ReadAll(r.Body)
			_ = r.Body.Close()
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/terrapod/v1/vcs-connections":
			// Pretend the operator pre-wired one Terrapod-side
			// connection that matches the atlantis-derived host.
			_, _ = w.Write([]byte(`{"data":[{"id":"vcs-fixt","type":"vcs-connections","attributes":{"name":"github-prod","provider":"github","server-url":"https://github.com","has-token":true}}]}`))
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/workspaces"):
			workspacesCreated++
			id := "ws-" + intToStr(workspacesCreated)
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"` + id + `","type":"workspaces","attributes":{"name":"app"}}}`))
		default:
			http.Error(w, "unhandled "+r.Method+" "+r.URL.Path, http.StatusNotFound)
		}
	}))
	defer srv.Close()

	// ── Apply (the actual end-to-end exercise) ────────────────────
	plan, _, err := loadAtlantisPlan(cloneDir, "", atlantis.StateOptions{})
	if err != nil {
		t.Fatalf("loadAtlantisPlan: %v", err)
	}
	if len(plan.Workspaces) != 2 {
		t.Fatalf("expected 2 workspaces in plan, got %d", len(plan.Workspaces))
	}

	stateFile := filepath.Join(t.TempDir(), "migration-state.json")
	c, err := terrapod.NewClient(terrapod.Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}

	state := &framework.State{}
	w := writer.New(c, state, stateFile)
	// The fake server returns a single existing VCS connection that
	// matches the atlantis-derived github.com host; pretend the
	// operator has already wired it up in Terrapod.
	connByRef := map[string]string{
		plan.VCSConnections[0].SourceID: "vcs-fixt",
	}
	report, err := w.Run(context.Background(), plan, writer.Options{
		DryRun:               false,
		VCSConnectionIDByRef: connByRef,
	})
	if err != nil {
		t.Fatalf("writer.Run: %v", err)
	}

	// ── Assertions ────────────────────────────────────────────────
	if len(report.Errors) != 0 {
		t.Errorf("expected no errors, got: %v", report.Errors)
	}
	if workspacesCreated != 2 {
		t.Errorf("expected 2 workspaces created, got %d", workspacesCreated)
	}
	// Connection lookup should have matched the pre-existing
	// Terrapod connection — surfacing as "matched", not "missing".
	if len(report.Connections) != 1 || report.Connections[0].State != "matched" {
		t.Errorf("expected one matched connection, got: %+v", report.Connections)
	}

	// State file should now exist and list both workspaces.
	loaded, err := framework.Load(stateFile)
	if err != nil || loaded == nil {
		t.Fatalf("Load state: %v / %v", loaded, err)
	}
	if len(loaded.Workspaces) != 2 {
		t.Errorf("state file workspaces: %+v", loaded.Workspaces)
	}
	for _, wsRec := range loaded.Workspaces {
		if wsRec.TerrapodID == "" {
			t.Errorf("workspace %q has no terrapod_id in state: %+v", wsRec.SourceName, wsRec)
		}
		if wsRec.State != "created" {
			t.Errorf("workspace %q state = %q, want \"created\"", wsRec.SourceName, wsRec.State)
		}
	}

	// State file should also remember the source host (for the rewriter).
	if loaded.SourceHost == "" {
		t.Errorf("state file missing source_host: %+v", loaded)
	}

	// Marshal the report to JSON to confirm it serialises (the
	// --json flag path).
	if _, err := json.Marshal(report); err != nil {
		t.Errorf("report doesn't marshal: %v", err)
	}
}

// intToStr is a tiny helper — strconv is overkill for one-call.
func intToStr(n int) string {
	if n == 0 {
		return "0"
	}
	var buf [12]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	return string(buf[i:])
}
