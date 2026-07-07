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
	t                   *testing.T
	connectionsCreated  int
	workspacesCreated   int
	variablesCreated    int
	varsetsCreated      int
	varsetVarsCreated   int
	varsetAssignments   int
	runTriggersCreated  int
	notificationCreated int
	agentPoolsCreated   int
	workspacePatches    int
	gpgKeysCreated      int
	// lastNotificationBody records the most recent notification-create body
	lastNotificationBody []byte
	// lastWorkspaceBody records the most recent workspace-create body
	// so tests can verify field round-tripping.
	lastWorkspaceBody []byte
	// lastVarsetBody records the most recent varset-create body.
	lastVarsetBody []byte
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
		// Varset routes must be matched BEFORE the generic "/workspaces"
		// and "/vars" suffix cases: the varset→workspace assignment ends
		// in "/relationships/workspaces" and the varset variable POST
		// ends in "/relationships/vars".
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/varsets"):
			fs.varsetsCreated++
			fs.lastVarsetBody = body
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"varset-fixt","type":"varsets","attributes":{"name":"nm","global":false,"priority":false}}}`))
		case r.Method == http.MethodPost && strings.Contains(r.URL.Path, "/relationships/vars"):
			fs.varsetVarsCreated++
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"vsv-fixt","type":"vars","attributes":{"key":"k","value":"v","category":"terraform"}}}`))
		case r.Method == http.MethodPost && strings.Contains(r.URL.Path, "/relationships/workspaces"):
			fs.varsetAssignments++
			w.WriteHeader(http.StatusNoContent)
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/run-triggers"):
			fs.runTriggersCreated++
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"rt-fixt","type":"run-triggers","attributes":{"workspace-id":"ws-fixt","sourceable-id":"ws-fixt"}}}`))
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/notification-configurations"):
			fs.notificationCreated++
			fs.lastNotificationBody = body
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"nc-fixt","type":"notification-configurations","attributes":{"name":"nm","destination-type":"generic","enabled":true}}}`))
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/agent-pools"):
			fs.agentPoolsCreated++
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"ap-fixt","type":"agent-pools","attributes":{"name":"nm"}}}`))
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/gpg-keys"):
			fs.gpgKeysCreated++
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"gpg-fixt","type":"gpg-keys","attributes":{"key-id":"ABC123"}}}`))
		case r.Method == http.MethodPatch && strings.Contains(r.URL.Path, "/api/v2/workspaces/"):
			fs.workspacePatches++
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"data":{"id":"ws-fixt","type":"workspaces","attributes":{"name":"app","agent-pool-id":"ap-fixt"}}}`))
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

func TestWriter_Apply_CreatesVariableSet(t *testing.T) {
	fs, c := newFakeServer(t)
	state := &framework.State{}
	w := New(c, state, "")

	plan := ir.Plan{
		Source: "tfe",
		Workspaces: []ir.Workspace{
			{SourceID: "ws-src-1", Name: "app"},
		},
		VariableSets: []ir.VariableSet{
			{
				SourceID: "vs-src-1",
				Name:     "global-tags",
				Variables: []ir.Variable{
					{Key: "environment", Value: "prod", Category: "terraform"},
					{Key: "api_key", Sensitive: true, Category: "env"},
				},
				WorkspaceRefs: []string{"ws-src-1"},
			},
		},
	}

	report, err := w.Run(t.Context(), plan, Options{})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if len(report.Errors) != 0 {
		t.Fatalf("expected no errors, got: %+v", report.Errors)
	}
	if fs.varsetsCreated != 1 || fs.varsetVarsCreated != 2 || fs.varsetAssignments != 1 {
		t.Errorf("varset API calls: sets=%d vars=%d assigns=%d", fs.varsetsCreated, fs.varsetVarsCreated, fs.varsetAssignments)
	}
	if len(report.VariableSets) != 1 {
		t.Fatalf("expected 1 varset outcome, got %d", len(report.VariableSets))
	}
	vo := report.VariableSets[0]
	if vo.State != "created" || vo.TerrapodID != "varset-fixt" {
		t.Errorf("varset outcome: %+v", vo)
	}
	if vo.Assignments != 1 || len(vo.Unresolved) != 0 {
		t.Errorf("assignments=%d unresolved=%v", vo.Assignments, vo.Unresolved)
	}
	// The sensitive var must be created as needs_value (empty), never
	// with a read-back value.
	var needsValue int
	for _, v := range vo.VarOutcomes {
		if v.State == "needs_value" {
			needsValue++
		}
	}
	if needsValue != 1 {
		t.Errorf("expected 1 needs_value var outcome, got %d (%+v)", needsValue, vo.VarOutcomes)
	}
	// State-file mapping + provenance gate recorded.
	rec := state.VarsetBySourceID("vs-src-1")
	if rec == nil || rec.TerrapodID != "varset-fixt" || !rec.CreatedByMigration {
		t.Errorf("varset state record: %+v", rec)
	}
}

func TestWriter_VariableSet_Idempotent_Resume(t *testing.T) {
	fs, c := newFakeServer(t)
	state := &framework.State{}
	w := New(c, state, "")

	plan := ir.Plan{
		Source:     "tfe",
		Workspaces: []ir.Workspace{{SourceID: "ws-src-1", Name: "app"}},
		VariableSets: []ir.VariableSet{
			{SourceID: "vs-src-1", Name: "global-tags", Global: true,
				Variables: []ir.Variable{{Key: "environment", Value: "prod", Category: "terraform"}}},
		},
	}

	if _, err := w.Run(t.Context(), plan, Options{}); err != nil {
		t.Fatalf("first Run: %v", err)
	}
	// Second run against the same state must NOT re-create the varset —
	// the recorded TerrapodID makes it a reuse.
	report, err := w.Run(t.Context(), plan, Options{})
	if err != nil {
		t.Fatalf("second Run: %v", err)
	}
	if fs.varsetsCreated != 1 {
		t.Errorf("varset re-created on resume: varsetsCreated=%d (want 1)", fs.varsetsCreated)
	}
	if len(report.VariableSets) != 1 || report.VariableSets[0].State != "reused" {
		t.Errorf("expected reused varset outcome, got: %+v", report.VariableSets)
	}
}

func TestWriter_DryRun_VariableSet_NoAPICalls(t *testing.T) {
	fs, c := newFakeServer(t)
	state := &framework.State{}
	w := New(c, state, "")

	plan := ir.Plan{
		Source:     "tfe",
		Workspaces: []ir.Workspace{{SourceID: "ws-src-1", Name: "app"}},
		VariableSets: []ir.VariableSet{
			{SourceID: "vs-src-1", Name: "global-tags",
				Variables:     []ir.Variable{{Key: "environment", Value: "prod", Category: "terraform"}},
				WorkspaceRefs: []string{"ws-src-1"}},
		},
	}

	report, err := w.Run(t.Context(), plan, Options{DryRun: true})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if fs.varsetsCreated != 0 || fs.varsetVarsCreated != 0 || fs.varsetAssignments != 0 {
		t.Errorf("dry-run touched varset API: sets=%d vars=%d assigns=%d",
			fs.varsetsCreated, fs.varsetVarsCreated, fs.varsetAssignments)
	}
	if len(report.VariableSets) != 1 || report.VariableSets[0].State != "planned" {
		t.Errorf("varset outcome: %+v", report.VariableSets)
	}
	// The workspace ref resolves to a planned workspace, so the dry-run
	// reports one planned assignment (not unresolved).
	if report.VariableSets[0].Assignments != 1 {
		t.Errorf("planned assignments = %d (want 1)", report.VariableSets[0].Assignments)
	}
}

func TestWriter_Apply_RunTrigger_CreatesInScope_SkipsOutOfScope(t *testing.T) {
	fs, c := newFakeServer(t)
	state := &framework.State{}
	w := New(c, state, "")

	plan := ir.Plan{
		Source: "tfe",
		Workspaces: []ir.Workspace{
			{SourceID: "ws-src", Name: "networking"},
			{SourceID: "ws-dst", Name: "app"},
		},
		RunTriggers: []ir.RunTrigger{
			// Both endpoints migrated → created.
			{SourceWorkspaceRef: "ws-src", DestinationWorkspaceRef: "ws-dst", SourceName: "networking", DestinationName: "app"},
			// Source outside the migration scope → skipped, not created.
			{SourceWorkspaceRef: "ws-external", DestinationWorkspaceRef: "ws-dst", SourceName: "external", DestinationName: "app"},
		},
	}

	report, err := w.Run(t.Context(), plan, Options{})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if len(report.Errors) != 0 {
		t.Fatalf("expected no errors, got: %+v", report.Errors)
	}
	if fs.runTriggersCreated != 1 {
		t.Errorf("expected 1 run-trigger POST, got %d", fs.runTriggersCreated)
	}
	if len(report.RunTriggers) != 2 {
		t.Fatalf("expected 2 run-trigger outcomes, got %d", len(report.RunTriggers))
	}
	if report.RunTriggers[0].State != "created" || report.RunTriggers[0].TerrapodID != "rt-fixt" {
		t.Errorf("in-scope trigger: %+v", report.RunTriggers[0])
	}
	if report.RunTriggers[1].State != "skipped" {
		t.Errorf("out-of-scope trigger should be skipped: %+v", report.RunTriggers[1])
	}
	// State records the created trigger with the provenance gate set.
	rec := state.RunTriggerByPair("ws-src", "ws-dst")
	if rec == nil || rec.TerrapodID != "rt-fixt" || !rec.CreatedByMigration {
		t.Errorf("run-trigger state record: %+v", rec)
	}

	// Idempotent: a second run reuses (no new POST).
	fs.runTriggersCreated = 0
	report2, err := w.Run(t.Context(), plan, Options{})
	if err != nil {
		t.Fatalf("second Run: %v", err)
	}
	if fs.runTriggersCreated != 0 {
		t.Errorf("run-trigger re-created on resume: %d", fs.runTriggersCreated)
	}
	if report2.RunTriggers[0].State != "reused" {
		t.Errorf("expected reused, got: %+v", report2.RunTriggers[0])
	}
}

func TestWriter_Apply_Notification_CreatesInScope_SkipsOutOfScope(t *testing.T) {
	fs, c := newFakeServer(t)
	state := &framework.State{}
	w := New(c, state, "")

	plan := ir.Plan{
		Source: "tfe",
		Workspaces: []ir.Workspace{
			{SourceID: "ws-dst", Name: "app"},
		},
		Notifications: []ir.NotificationConfiguration{
			// Destination migrated → created.
			{WorkspaceRef: "ws-dst", Name: "slack-alerts", DestinationType: "generic", URL: "https://hooks.example/x", Enabled: true, Triggers: []string{"run:errored"}, NeedsToken: true, WorkspaceName: "app"},
			// Destination outside the migration scope → skipped.
			{WorkspaceRef: "ws-external", Name: "orphan", DestinationType: "slack", URL: "https://hooks.example/y", WorkspaceName: "external"},
		},
	}

	report, err := w.Run(t.Context(), plan, Options{})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if len(report.Errors) != 0 {
		t.Fatalf("expected no errors, got: %+v", report.Errors)
	}
	if fs.notificationCreated != 1 {
		t.Errorf("expected 1 notification POST, got %d", fs.notificationCreated)
	}
	if len(report.Notifications) != 2 {
		t.Fatalf("expected 2 notification outcomes, got %d", len(report.Notifications))
	}
	if report.Notifications[0].State != "created" || report.Notifications[0].TerrapodID != "nc-fixt" {
		t.Errorf("in-scope notification: %+v", report.Notifications[0])
	}
	if !report.Notifications[0].NeedsToken {
		t.Errorf("expected NeedsToken flagged on generic webhook: %+v", report.Notifications[0])
	}
	if report.Notifications[1].State != "skipped" {
		t.Errorf("out-of-scope notification should be skipped: %+v", report.Notifications[1])
	}
	// The create body must NOT carry a non-empty token (source never
	// returns it, so we migrate the config with an empty token).
	var env struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	_ = json.Unmarshal(fs.lastNotificationBody, &env)
	if tok, ok := env.Data.Attributes["token"].(string); ok && tok != "" {
		t.Errorf("notification create body leaked a token: %q", tok)
	}
	// State records the created config with the provenance gate set.
	rec := state.NotificationByWorkspaceAndName("ws-dst", "slack-alerts")
	if rec == nil || rec.TerrapodID != "nc-fixt" || !rec.CreatedByMigration {
		t.Errorf("notification state record: %+v", rec)
	}

	// Idempotent: a second run reuses (no new POST).
	fs.notificationCreated = 0
	report2, err := w.Run(t.Context(), plan, Options{})
	if err != nil {
		t.Fatalf("second Run: %v", err)
	}
	if fs.notificationCreated != 0 {
		t.Errorf("notification re-created on resume: %d", fs.notificationCreated)
	}
	if report2.Notifications[0].State != "reused" {
		t.Errorf("expected reused, got: %+v", report2.Notifications[0])
	}
}

func TestWriter_Apply_AgentPool_CreatesAndRepointsWorkspaces(t *testing.T) {
	fs, c := newFakeServer(t)
	state := &framework.State{}
	w := New(c, state, "")

	plan := ir.Plan{
		Source: "tfe",
		Workspaces: []ir.Workspace{
			{SourceID: "ws-a", Name: "app", ExecutionMode: "agent"},
		},
		AgentPools: []ir.AgentPool{
			// One in-scope member (ws-a), one out-of-scope (ws-ext).
			{SourceID: "pool-1", Name: "aws-prod", WorkspaceRefs: []string{"ws-a", "ws-ext"}},
		},
	}

	report, err := w.Run(t.Context(), plan, Options{})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if len(report.Errors) != 0 {
		t.Fatalf("expected no errors, got: %+v", report.Errors)
	}
	if fs.agentPoolsCreated != 1 {
		t.Errorf("expected 1 agent-pool POST, got %d", fs.agentPoolsCreated)
	}
	// Only the in-scope member workspace gets re-pointed (one PATCH).
	if fs.workspacePatches != 1 {
		t.Errorf("expected 1 workspace PATCH, got %d", fs.workspacePatches)
	}
	if len(report.AgentPools) != 1 {
		t.Fatalf("expected 1 agent-pool outcome, got %d", len(report.AgentPools))
	}
	ap := report.AgentPools[0]
	if ap.State != "created" || ap.TerrapodID != "ap-fixt" {
		t.Errorf("agent-pool outcome: %+v", ap)
	}
	if ap.Assignments != 1 {
		t.Errorf("expected 1 assignment, got %d", ap.Assignments)
	}
	if len(ap.Unresolved) != 1 || ap.Unresolved[0] != "ws-ext" {
		t.Errorf("expected ws-ext unresolved, got %+v", ap.Unresolved)
	}
	// State records the created pool with the provenance gate set.
	rec := state.AgentPoolBySourceID("pool-1")
	if rec == nil || rec.TerrapodID != "ap-fixt" || !rec.CreatedByMigration {
		t.Errorf("agent-pool state record: %+v", rec)
	}

	// Idempotent: a second run reuses (no new pool POST).
	fs.agentPoolsCreated = 0
	report2, err := w.Run(t.Context(), plan, Options{})
	if err != nil {
		t.Fatalf("second Run: %v", err)
	}
	if fs.agentPoolsCreated != 0 {
		t.Errorf("agent-pool re-created on resume: %d", fs.agentPoolsCreated)
	}
	if report2.AgentPools[0].State != "reused" {
		t.Errorf("expected reused, got: %+v", report2.AgentPools[0])
	}
}

func TestWriter_Apply_GPGKey_CreatesAndIsIdempotent(t *testing.T) {
	fs, c := newFakeServer(t)
	state := &framework.State{}
	w := New(c, state, "")

	plan := ir.Plan{
		Source: "tfe",
		GPGKeys: []ir.GPGKey{
			{SourceID: "gk-1", KeyID: "ABC123", ASCIIArmor: "-----BEGIN PGP PUBLIC KEY BLOCK-----\n...\n-----END PGP PUBLIC KEY BLOCK-----"},
		},
	}

	report, err := w.Run(t.Context(), plan, Options{})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if len(report.Errors) != 0 {
		t.Fatalf("expected no errors, got: %+v", report.Errors)
	}
	if fs.gpgKeysCreated != 1 {
		t.Errorf("expected 1 gpg-key POST, got %d", fs.gpgKeysCreated)
	}
	if len(report.GPGKeys) != 1 {
		t.Fatalf("expected 1 gpg-key outcome, got %d", len(report.GPGKeys))
	}
	gk := report.GPGKeys[0]
	if gk.State != "created" || gk.TerrapodID != "gpg-fixt" {
		t.Errorf("gpg-key outcome: %+v", gk)
	}
	// State records the created key with the provenance gate set.
	rec := state.GPGKeyBySourceID("gk-1")
	if rec == nil || rec.TerrapodID != "gpg-fixt" || !rec.CreatedByMigration {
		t.Errorf("gpg-key state record: %+v", rec)
	}

	// Idempotent: a second run reuses (no new POST).
	fs.gpgKeysCreated = 0
	report2, err := w.Run(t.Context(), plan, Options{})
	if err != nil {
		t.Fatalf("second Run: %v", err)
	}
	if fs.gpgKeysCreated != 0 {
		t.Errorf("gpg-key re-created on resume: %d", fs.gpgKeysCreated)
	}
	if report2.GPGKeys[0].State != "reused" {
		t.Errorf("expected reused, got: %+v", report2.GPGKeys[0])
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

func TestWriter_Apply_SetsCreatedByMigrationAndVarCount(t *testing.T) {
	_, c := newFakeServer(t)
	state := &framework.State{}
	w := New(c, state, "")
	plan := ir.Plan{
		Source: "tfe",
		Workspaces: []ir.Workspace{{
			SourceID: "ws-src-1",
			Name:     "app",
			Variables: []ir.Variable{
				{Key: "region", Value: "eu-west-1", Category: "terraform"},
				{Key: "tier", Value: "prod", Category: "terraform"},
			},
		}},
	}
	if _, err := w.Run(t.Context(), plan, Options{DryRun: false}); err != nil {
		t.Fatalf("Run: %v", err)
	}
	rec := state.WorkspaceBySourceID("ws-src-1")
	if rec == nil || !rec.CreatedByMigration {
		t.Fatalf("CreatedByMigration must be true after a real create: %+v", rec)
	}
	if rec.ExpectedVarCount != 2 {
		t.Fatalf("ExpectedVarCount = %d, want 2", rec.ExpectedVarCount)
	}
}

func TestWriter_Reused_DoesNotSetCreatedByMigration(t *testing.T) {
	_, c := newFakeServer(t)
	// Pre-seed a reused workspace (e.g. apply --workspace direct mode):
	// it has a TerrapodID but was NOT created by the migration.
	state := &framework.State{Workspaces: []framework.WorkspaceRecord{
		{SourceID: "direct:app", SourceName: "app", TerrapodID: "ws-pre", State: "created", CreatedByMigration: false},
	}}
	w := New(c, state, "")
	plan := ir.Plan{Source: "atlantis", Workspaces: []ir.Workspace{{SourceID: "direct:app", Name: "app"}}}
	if _, err := w.Run(t.Context(), plan, Options{DryRun: false}); err != nil {
		t.Fatalf("Run: %v", err)
	}
	if rec := state.WorkspaceBySourceID("direct:app"); rec.CreatedByMigration {
		t.Fatal("reused workspace must NOT be flagged CreatedByMigration — rollback would delete an operator's pre-existing workspace")
	}
}

func TestWriter_DryRun_PlansStateWithoutUpload(t *testing.T) {
	fs, c := newFakeServer(t)
	state := &framework.State{}
	w := New(c, state, "")
	reader := func(_ context.Context, _ string) ([]byte, string, int64, error) {
		return []byte(`{"serial":5,"lineage":"lin-1"}`), "lin-1", 5, nil
	}
	plan := ir.Plan{Source: "tfe", Workspaces: []ir.Workspace{{SourceID: "ws-1", Name: "app"}}}
	report, err := w.Run(t.Context(), plan, Options{DryRun: true, StateForWorkspace: reader})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if fs.workspacesCreated != 0 {
		t.Fatalf("dry-run created a workspace")
	}
	so := report.Workspaces[0].StateOutcome
	if so == nil || so.State != "planned" {
		t.Fatalf("dry-run should report planned state, got %+v", so)
	}
	if so.Serial != 5 || so.Lineage != "lin-1" {
		t.Fatalf("planned state metadata wrong: %+v", so)
	}
}
