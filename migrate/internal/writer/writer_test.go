package writer

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/migrate/internal/framework"
	"github.com/mattrobinsonsre/terrapod/migrate/internal/ir"
)

// fakeTerrapodServer is a minimal httptest server that responds to
// every Terrapod endpoint the writer touches. Each handler counts
// how many times it was hit so tests can assert on the API call
// pattern (e.g. "no workspace POST in dry-run mode").
type fakeTerrapodServer struct {
	t                  *testing.T
	connectionsCreated int
	workspacesCreated  int
	variablesCreated   int
	// lastWorkspaceBody records the most recent workspace-create body
	// so tests can verify field round-tripping.
	lastWorkspaceBody []byte
}

func newFakeServer(t *testing.T) (*fakeTerrapodServer, *terrapod.Client) {
	t.Helper()
	fs := &fakeTerrapodServer{t: t}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body []byte
		if r.Body != nil {
			body, _ = io.ReadAll(r.Body)
			_ = r.Body.Close()
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/terrapod/v1/vcs-connections":
			fs.connectionsCreated++
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"vcs-fixt","type":"vcs-connections","attributes":{"name":"github","provider":"github","has-token":true}}}`))
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/workspaces"):
			fs.workspacesCreated++
			fs.lastWorkspaceBody = body
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"ws-fixt","type":"workspaces","attributes":{"name":"app"}}}`))
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/vars"):
			fs.variablesCreated++
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"var-fixt","type":"vars","attributes":{"key":"k","value":"v","category":"terraform"}}}`))
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

func TestWriter_DryRun_NoAPICalls(t *testing.T) {
	fs, c := newFakeServer(t)
	state := &framework.State{}
	w := New(c, state, "") // in-memory state

	plan := ir.Plan{
		Source: "atlantis",
		VCSConnections: []ir.VCSConnection{
			{SourceID: "src-1", Name: "github", Provider: "github"},
		},
		Workspaces: []ir.Workspace{
			{
				SourceID: "ws-src-1",
				Name:     "app",
				Variables: []ir.Variable{
					{Key: "region", Value: "eu-west-1", Category: "terraform"},
					{Key: "db_password", Sensitive: true, Category: "terraform"},
				},
			},
		},
	}

	report, err := w.Run(t.Context(), plan, Options{DryRun: true})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if !report.DryRun {
		t.Error("report.DryRun should be true")
	}
	if fs.connectionsCreated != 0 || fs.workspacesCreated != 0 || fs.variablesCreated != 0 {
		t.Errorf("dry-run touched the API: conns=%d ws=%d vars=%d",
			fs.connectionsCreated, fs.workspacesCreated, fs.variablesCreated)
	}
	if len(report.Workspaces) != 1 || report.Workspaces[0].State != "planned" {
		t.Errorf("workspace outcome: %+v", report.Workspaces)
	}
	// Variables should appear in the outcome but with State="planned".
	if len(report.Workspaces[0].VarOutcomes) != 2 {
		t.Errorf("expected 2 var outcomes, got %d", len(report.Workspaces[0].VarOutcomes))
	}
}

func TestWriter_Apply_CreatesEverything(t *testing.T) {
	fs, c := newFakeServer(t)
	state := &framework.State{}
	w := New(c, state, "")

	plan := ir.Plan{
		Source: "tfe",
		VCSConnections: []ir.VCSConnection{
			{SourceID: "src-1", Name: "github", Provider: "github"},
		},
		Workspaces: []ir.Workspace{
			{
				SourceID:         "ws-src-1",
				Name:             "app",
				VCSConnectionRef: "src-1",
				Variables: []ir.Variable{
					{Key: "region", Value: "eu-west-1", Category: "terraform"},
				},
			},
		},
	}

	opts := Options{
		// Pretend the operator already wired a Terrapod-side VCS
		// connection that matches the plan's src-1 reference.
		VCSConnectionIDByRef: map[string]string{"src-1": "vcs-existing"},
	}
	report, err := w.Run(t.Context(), plan, opts)
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	// The migrator no longer creates VCS connections — only matches
	// existing ones. Expect zero connection POSTs and exactly one
	// workspace + one variable.
	if fs.connectionsCreated != 0 || fs.workspacesCreated != 1 || fs.variablesCreated != 1 {
		t.Errorf("API calls: conns=%d ws=%d vars=%d", fs.connectionsCreated, fs.workspacesCreated, fs.variablesCreated)
	}
	if len(report.Errors) != 0 {
		t.Errorf("expected no errors, got: %+v", report.Errors)
	}
	if state.WorkspaceBySourceID("ws-src-1").TerrapodID == "" {
		// TerrapodID is recorded as the Workspace's ID — verify it propagated.
		// (The fake server returns "ws-fixt" for every workspace POST.)
		t.Errorf("workspace state record: %+v", state.WorkspaceBySourceID("ws-src-1"))
	}
}

func TestWriter_Apply_VCSConnectionRefResolution(t *testing.T) {
	// Verify the workspace's VCSConnectionRef ("src-1") is rewritten
	// to the Terrapod-side connection id created earlier in the same
	// Plan. The fake server's create-workspace endpoint records the
	// request body; we look for the relationship.
	fs, c := newFakeServer(t)
	w := New(c, &framework.State{}, "")

	plan := ir.Plan{
		Source: "atlantis",
		VCSConnections: []ir.VCSConnection{
			{SourceID: "src-1", Name: "github", Provider: "github"},
		},
		Workspaces: []ir.Workspace{
			{SourceID: "ws-src-1", Name: "app", VCSConnectionRef: "src-1"},
		},
	}
	opts := Options{
		VCSConnectionIDByRef: map[string]string{"src-1": "vcs-existing"},
	}
	_, err := w.Run(t.Context(), plan, opts)
	if err != nil {
		t.Fatal(err)
	}

	var doc struct {
		Data struct {
			Relationships map[string]any `json:"relationships"`
		} `json:"data"`
	}
	_ = json.Unmarshal(fs.lastWorkspaceBody, &doc)
	rel, ok := doc.Data.Relationships["vcs-connection"].(map[string]any)
	if !ok {
		t.Fatalf("vcs-connection relationship missing: %+v", doc.Data.Relationships)
	}
	data, ok := rel["data"].(map[string]any)
	if !ok || data["id"] != "vcs-existing" {
		t.Errorf("vcs-connection id not wired through: %+v", rel)
	}
}

func TestWriter_Apply_Idempotent_Resume(t *testing.T) {
	// First Run creates everything; second Run starts with the same
	// state and should report "reused" without re-hitting the API.
	fs, c := newFakeServer(t)
	state := &framework.State{}
	w := New(c, state, "")

	plan := ir.Plan{
		Source: "atlantis",
		Workspaces: []ir.Workspace{
			{SourceID: "ws-src-1", Name: "app"},
		},
	}
	opts := Options{}
	if _, err := w.Run(t.Context(), plan, opts); err != nil {
		t.Fatal(err)
	}
	if fs.workspacesCreated != 1 {
		t.Fatalf("expected 1 create on first run, got %d", fs.workspacesCreated)
	}

	// Second run — same state, same plan.
	w2 := New(c, state, "")
	report, err := w2.Run(t.Context(), plan, opts)
	if err != nil {
		t.Fatal(err)
	}
	if fs.workspacesCreated != 1 {
		t.Errorf("second run should have made 0 new creates, got %d total", fs.workspacesCreated)
	}
	if report.Workspaces[0].State != "reused" {
		t.Errorf("second run state: %q", report.Workspaces[0].State)
	}
}

func TestWriter_Apply_RecordsErrors(t *testing.T) {
	// A handler that returns 500 on every workspace POST — verifies
	// the writer surfaces the error in the Report.Errors aggregate
	// rather than aborting the whole migration.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/workspaces") {
			http.Error(w, `{"errors":[{"status":"500","detail":"boom"}]}`, http.StatusInternalServerError)
			return
		}
		http.Error(w, "unhandled", http.StatusNotFound)
	}))
	defer srv.Close()
	c, _ := terrapod.NewClient(terrapod.Options{BaseURL: srv.URL, Token: "t"})

	plan := ir.Plan{
		Source:     "atlantis",
		Workspaces: []ir.Workspace{{SourceID: "ws-src-1", Name: "app"}},
	}
	report, err := New(c, &framework.State{}, "").Run(t.Context(), plan, Options{})
	if err != nil {
		t.Fatal(err)
	}
	if len(report.Errors) == 0 {
		t.Errorf("expected errors in report, got none")
	}
	if report.Workspaces[0].State != "errored" {
		t.Errorf("workspace state: %q", report.Workspaces[0].State)
	}
}
