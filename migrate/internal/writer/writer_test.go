package writer

import (
	"context"
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

// fakeStateReader builds a StateReader that always returns the same
// (raw, lineage, serial) triple. Used by the safety-path tests
// below.
func fakeStateReader(raw []byte, lineage string, serial int64) StateReader {
	return func(_ context.Context, _ string) ([]byte, string, int64, error) {
		return raw, lineage, serial, nil
	}
}

// stateScenarioServer is a fake Terrapod that supports the
// state-version create/upload/get flow with configurable behaviour:
//   - createStatus / createBody : what to return on POST /state-versions
//   - putStatus              : what to return on PUT /state-versions/{id}/content
//   - currentSV              : what GetCurrentStateVersion returns (nil → 404)
//   - rollbackOK             : if false, the DELETE returns 500
//
// Tests use it to drive each safety branch without standing up a
// full Terrapod.
type stateScenarioServer struct {
	createStatus  int
	createBody    string
	putStatus     int
	currentSV     *terrapod.StateVersion
	rollbackOK    bool
	createCalled  int
	putCalled     int
	deleteCalled  int
	currentCalled int
}

func newStateScenarioClient(t *testing.T, s *stateScenarioServer) *terrapod.Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/state-versions"):
			s.createCalled++
			body := s.createBody
			if body == "" {
				body = `{"data":{"id":"sv-new","type":"state-versions","attributes":{"serial":1,"lineage":"L1","md5":""}}}`
			}
			w.WriteHeader(s.createStatus)
			_, _ = w.Write([]byte(body))
		case r.Method == http.MethodPut && strings.HasSuffix(r.URL.Path, "/content"):
			s.putCalled++
			w.WriteHeader(s.putStatus)
		case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/current-state-version"):
			s.currentCalled++
			if s.currentSV == nil {
				http.Error(w, `{"errors":[{"status":"404","detail":"none"}]}`, http.StatusNotFound)
				return
			}
			_, _ = w.Write([]byte(stateVersionJSON(s.currentSV)))
		case r.Method == http.MethodDelete && strings.Contains(r.URL.Path, "/state-versions/") && strings.HasSuffix(r.URL.Path, "/manage"):
			s.deleteCalled++
			if !s.rollbackOK {
				http.Error(w, `{"errors":[{"status":"500","detail":"rollback boom"}]}`, http.StatusInternalServerError)
				return
			}
			w.WriteHeader(http.StatusNoContent)
		default:
			http.Error(w, "unhandled "+r.Method+" "+r.URL.Path, http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	c, err := terrapod.NewClient(terrapod.Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func stateVersionJSON(sv *terrapod.StateVersion) string {
	return `{"data":{"id":"` + sv.ID + `","type":"state-versions","attributes":{"serial":` +
		fmtInt(sv.Serial) + `,"lineage":"` + sv.Lineage + `","md5":"` + sv.MD5 + `","state-size":` + fmtInt(sv.StateSize) + `}}}`
}

func fmtInt(n int64) string {
	if n == 0 {
		return "0"
	}
	var b []byte
	neg := n < 0
	if neg {
		n = -n
	}
	for n > 0 {
		b = append([]byte{byte('0' + n%10)}, b...)
		n /= 10
	}
	if neg {
		b = append([]byte{'-'}, b...)
	}
	return string(b)
}

// TestApplyState_LineageMismatch_RefusesUpload exercises the round-1
// pre-check that prevents an unrelated state lineage from being
// overwritten silently.
func TestApplyState_LineageMismatch_RefusesUpload(t *testing.T) {
	scenario := &stateScenarioServer{
		currentSV: &terrapod.StateVersion{ID: "sv-existing", Serial: 5, Lineage: "L_OTHER", StateSize: 1234, MD5: "abc"},
	}
	c := newStateScenarioClient(t, scenario)
	w := New(c, &framework.State{}, "")

	out := w.applyState(t.Context(), "ws-1", "src-1", fakeStateReader([]byte("state-body"), "L_SOURCE", 7))
	if out.State != "errored" {
		t.Fatalf("expected errored, got %q (err=%q)", out.State, out.Error)
	}
	if !strings.Contains(out.Error, "lineage") {
		t.Errorf("error should mention lineage: %q", out.Error)
	}
	if scenario.createCalled != 0 {
		t.Errorf("must not have called CreateStateVersion on lineage mismatch")
	}
}

// TestApplyState_DestSerialAhead_RefusesUpload exercises the
// "destination has advanced" guard. Even with matching lineage we
// refuse rather than roll back operator work.
func TestApplyState_DestSerialAhead_RefusesUpload(t *testing.T) {
	scenario := &stateScenarioServer{
		currentSV: &terrapod.StateVersion{ID: "sv-existing", Serial: 10, Lineage: "L_SOURCE", StateSize: 1234, MD5: "abc"},
	}
	c := newStateScenarioClient(t, scenario)
	w := New(c, &framework.State{}, "")

	out := w.applyState(t.Context(), "ws-1", "src-1", fakeStateReader([]byte("state-body"), "L_SOURCE", 7))
	if out.State != "errored" || !strings.Contains(out.Error, "serial") {
		t.Fatalf("expected serial-advanced error, got %q / %q", out.State, out.Error)
	}
	if scenario.createCalled != 0 {
		t.Errorf("must not upload when destination is ahead")
	}
}

// TestApplyState_PreCheckTransientError_HardFails verifies that a
// network/5xx failure on the pre-check (NOT a 404) is treated as a
// hard error rather than "no state, proceed".
func TestApplyState_PreCheckTransientError_HardFails(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Every request 500s — including the pre-check GET.
		http.Error(w, `{"errors":[{"status":"500","detail":"flake"}]}`, http.StatusInternalServerError)
	}))
	defer srv.Close()
	c, _ := terrapod.NewClient(terrapod.Options{BaseURL: srv.URL, Token: "t", MaxRetries: 0})
	w := New(c, &framework.State{}, "")

	out := w.applyState(t.Context(), "ws-1", "src-1", fakeStateReader([]byte("state-body"), "L_SOURCE", 7))
	if out.State != "errored" {
		t.Fatalf("expected errored on transient pre-check failure, got %q", out.State)
	}
	if !strings.Contains(out.Error, "pre-check") {
		t.Errorf("error should mention pre-check: %q", out.Error)
	}
}

// TestApplyState_EmptyPlaceholder_409_RefusesAsUnchanged is the
// round-2 P0 regression test. A prior run's orphan rollback failed,
// leaving a state-size=0 row at the source's serial. The 409 path
// MUST NOT mark this as `unchanged` — that would claim success
// while the workspace points at zero-byte state.
func TestApplyState_EmptyPlaceholder_409_RefusesAsUnchanged(t *testing.T) {
	scenario := &stateScenarioServer{
		createStatus: http.StatusConflict,
		createBody:   `{"errors":[{"status":"409","detail":"serial exists"}]}`,
		currentSV: &terrapod.StateVersion{
			ID: "sv-orphan", Serial: 7, Lineage: "L_SOURCE", StateSize: 0, MD5: "",
		},
	}
	c := newStateScenarioClient(t, scenario)
	w := New(c, &framework.State{}, "")

	out := w.applyState(t.Context(), "ws-1", "src-1", fakeStateReader([]byte("state-body"), "L_SOURCE", 7))
	if out.State != "errored" {
		t.Fatalf("expected errored on empty-placeholder 409, got %q (err=%q)", out.State, out.Error)
	}
	if !strings.Contains(out.Error, "orphan") {
		t.Errorf("error should mention orphan placeholder: %q", out.Error)
	}
}

// TestApplyState_FreshWorkspace_NotFound_Proceeds verifies the
// pre-check correctly treats a 404 from GetCurrentStateVersion as
// "fresh workspace, OK to proceed" rather than a hard fail.
func TestApplyState_FreshWorkspace_NotFound_Proceeds(t *testing.T) {
	scenario := &stateScenarioServer{
		createStatus: http.StatusCreated,
		createBody:   `{"data":{"id":"sv-new","type":"state-versions","attributes":{"serial":1,"lineage":"L_SOURCE","state-size":11}}}`,
		putStatus:    http.StatusOK,
		currentSV:    nil, // → 404
	}
	c := newStateScenarioClient(t, scenario)
	w := New(c, &framework.State{}, "")

	out := w.applyState(t.Context(), "ws-1", "src-1", fakeStateReader([]byte("state-body"), "L_SOURCE", 1))
	if out.State != "uploaded" {
		t.Fatalf("expected uploaded on fresh workspace, got %q (err=%q)", out.State, out.Error)
	}
	if scenario.putCalled != 1 {
		t.Errorf("expected one PUT, got %d", scenario.putCalled)
	}
}

// TestApplyState_UploadFails_OrphanRollback verifies that when the
// /content PUT fails, CreateAndUploadState's rollback fires and
// the orphan record is DELETEd.
func TestApplyState_UploadFails_OrphanRollback(t *testing.T) {
	scenario := &stateScenarioServer{
		createStatus: http.StatusCreated,
		createBody:   `{"data":{"id":"sv-new","type":"state-versions","attributes":{"serial":1,"lineage":"L_SOURCE","state-size":0}}}`,
		putStatus:    http.StatusInternalServerError,
		currentSV:    nil,
		rollbackOK:   true,
	}
	c := newStateScenarioClient(t, scenario)
	w := New(c, &framework.State{}, "")

	out := w.applyState(t.Context(), "ws-1", "src-1", fakeStateReader([]byte("state-body"), "L_SOURCE", 1))
	if out.State != "errored" {
		t.Fatalf("expected errored on upload failure, got %q", out.State)
	}
	if scenario.deleteCalled != 1 {
		t.Errorf("expected rollback DELETE, got %d calls", scenario.deleteCalled)
	}
}
