package terrapod

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// runAttrs is a representative planned-run payload with the Terrapod-native
// fields, nested action/timestamp blocks, and a mix of null + present nullables.
const runAttrs = `{
  "status":"planned","message":"nightly","is-destroy":false,"auto-apply":false,
  "plan-only":false,"source":"tfe-api","execution-backend":"tofu",
  "terraform-version":"1.9.0","terragrunt-enabled":false,
  "target-addrs":["aws_vpc.main"],"replace-addrs":[],
  "refresh-only":false,"refresh":true,"allow-empty-apply":false,
  "is-drift-detection":false,"has-changes":true,"has-json-output":true,
  "state-diverged":false,
  "has-cost-estimate":true,"cost-currency":"USD","cost-monthly-min":12.5,"cost-monthly-max":40.0,
  "peak-memory-bytes":536870912,"peak-cpu-usec":null,"runner-exit-code":0,
  "runner-exit-reason":"","workspace-name":"app",
  "vcs-commit-sha":"abc123","vcs-branch":"main","vcs-pull-request-number":null,
  "created-by":"alice@example.com",
  "created-at":"2026-07-17T10:00:00Z","updated-at":"2026-07-17T10:05:00Z",
  "actions":{"is-confirmable":true,"is-discardable":true,"is-cancelable":true,"is-retryable":false},
  "status-timestamps":{"plan-queued-at":"2026-07-17T10:00:00Z","planned-at":"2026-07-17T10:03:00Z"}
}`

func runPayload(id string) string {
	return `{"data":{"id":"` + id + `","type":"runs","attributes":` + runAttrs +
		`,"relationships":{"workspace":{"data":{"id":"ws-app","type":"workspaces"}}}}}`
}

func newRunsFixture(t *testing.T) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var reqBody []byte
		if r.Body != nil {
			reqBody, _ = io.ReadAll(r.Body)
			_ = r.Body.Close()
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		p := r.URL.Path
		switch {
		case r.Method == http.MethodPost && p == "/api/v2/runs":
			var doc struct {
				Data struct {
					Attributes    map[string]any `json:"attributes"`
					Relationships map[string]any `json:"relationships"`
				} `json:"data"`
			}
			_ = json.Unmarshal(reqBody, &doc)
			if _, ok := doc.Data.Attributes["plan-only"]; !ok {
				http.Error(w, "missing plan-only", http.StatusUnprocessableEntity)
				return
			}
			if doc.Data.Relationships["workspace"] == nil {
				http.Error(w, "missing workspace", http.StatusUnprocessableEntity)
				return
			}
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(runPayload("run-aaaa")))

		case r.Method == http.MethodGet && strings.HasSuffix(p, "/runs") && strings.Contains(p, "/workspaces/"):
			_, _ = w.Write([]byte(`{"data":[` +
				`{"id":"run-aaaa","type":"runs","attributes":` + runAttrs + `}]}`))

		case r.Method == http.MethodGet && strings.Contains(p, "/api/v2/runs/"):
			_, _ = w.Write([]byte(runPayload("run-aaaa")))

		case r.Method == http.MethodPost && strings.HasSuffix(p, "/actions/apply"):
			if strings.Contains(p, "run-locked") {
				http.Error(w, `{"errors":[{"status":"409","title":"Conflict","detail":"workspace is locked"}]}`, http.StatusConflict)
				return
			}
			_, _ = w.Write([]byte(runPayload("run-aaaa")))
		case r.Method == http.MethodPost && (strings.HasSuffix(p, "/actions/discard") || strings.HasSuffix(p, "/actions/cancel")):
			_, _ = w.Write([]byte(runPayload("run-aaaa")))

		// Plan JSON: 302 to a presigned URL that serves the raw JSON bytes.
		case r.Method == http.MethodGet && strings.HasSuffix(p, "/json-output"):
			if strings.Contains(p, "nooutput") {
				http.Error(w, `{"errors":[{"status":"404","title":"Not Found","detail":"JSON plan output not available"}]}`, http.StatusNotFound)
				return
			}
			http.Redirect(w, r, "/presigned/plan.json", http.StatusFound)
		case r.Method == http.MethodGet && p == "/presigned/plan.json":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"format_version":"1.2","resource_changes":[]}`))

		default:
			http.Error(w, "unhandled "+r.Method+" "+p, http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestCreateRun(t *testing.T) {
	c := newRunsFixture(t)
	yes := true
	run, err := c.CreateRun(t.Context(), CreateRunRequest{
		WorkspaceID: "ws-app",
		PlanOnly:    false,
		AutoApply:   &yes,
		Message:     "nightly",
	})
	if err != nil {
		t.Fatal(err)
	}
	if run.ID != "run-aaaa" || run.Status != "planned" {
		t.Errorf("run: %+v", run)
	}
}

func TestCreateRunRequiresWorkspace(t *testing.T) {
	c := newRunsFixture(t)
	if _, err := c.CreateRun(t.Context(), CreateRunRequest{PlanOnly: true}); err == nil {
		t.Fatal("expected error for missing workspace id")
	}
}

func TestGetRunParsesNativeFields(t *testing.T) {
	c := newRunsFixture(t)
	run, err := c.GetRun(t.Context(), "run-aaaa")
	if err != nil {
		t.Fatal(err)
	}
	// Workspace comes from the relationship.
	if run.WorkspaceID != "ws-app" {
		t.Errorf("workspace id: %q", run.WorkspaceID)
	}
	// Terrapod-native fields go-tfe can't model.
	if !run.HasJSONOutput {
		t.Error("expected has-json-output true")
	}
	if run.HasChanges == nil || !*run.HasChanges {
		t.Errorf("has-changes: %+v", run.HasChanges)
	}
	// Cost estimation fields (#871) surfaced on the run object.
	if !run.HasCostEstimate || run.CostCurrency != "USD" {
		t.Errorf("cost flags: has=%v ccy=%q", run.HasCostEstimate, run.CostCurrency)
	}
	if run.CostMonthlyMax == nil || *run.CostMonthlyMax != 40.0 {
		t.Errorf("cost-monthly-max: %+v", run.CostMonthlyMax)
	}
	if run.PeakMemoryBytes == nil || *run.PeakMemoryBytes != 536870912 {
		t.Errorf("peak-memory-bytes: %+v", run.PeakMemoryBytes)
	}
	// Present-but-zero nullable stays non-nil; null stays nil.
	if run.RunnerExitCode == nil || *run.RunnerExitCode != 0 {
		t.Errorf("runner-exit-code: %+v", run.RunnerExitCode)
	}
	if run.PeakCPUUsec != nil || run.VCSPullRequestNumber != nil {
		t.Errorf("expected nil for null nullables: cpu=%+v pr=%+v", run.PeakCPUUsec, run.VCSPullRequestNumber)
	}
	// Nested blocks.
	if !run.Actions.IsConfirmable || !run.Actions.IsCancelable || run.Actions.IsRetryable {
		t.Errorf("actions: %+v", run.Actions)
	}
	if run.StatusTimestamps.PlannedAt != "2026-07-17T10:03:00Z" {
		t.Errorf("status-timestamps: %+v", run.StatusTimestamps)
	}
	if len(run.TargetAddrs) != 1 || run.TargetAddrs[0] != "aws_vpc.main" {
		t.Errorf("target-addrs: %+v", run.TargetAddrs)
	}
}

func TestListWorkspaceRuns(t *testing.T) {
	c := newRunsFixture(t)
	runs, err := c.ListWorkspaceRuns(t.Context(), "ws-app", 1, 20)
	if err != nil {
		t.Fatal(err)
	}
	if len(runs) != 1 || runs[0].Status != "planned" {
		t.Errorf("runs: %+v", runs)
	}
}

func TestRunActions(t *testing.T) {
	c := newRunsFixture(t)
	for _, tc := range []struct {
		name string
		fn   func() (*Run, error)
	}{
		{"apply", func() (*Run, error) { return c.ApplyRun(t.Context(), "run-aaaa") }},
		{"discard", func() (*Run, error) { return c.DiscardRun(t.Context(), "run-aaaa") }},
		{"cancel", func() (*Run, error) { return c.CancelRun(t.Context(), "run-aaaa") }},
	} {
		run, err := tc.fn()
		if err != nil {
			t.Fatalf("%s: %v", tc.name, err)
		}
		if run.ID != "run-aaaa" {
			t.Errorf("%s: %+v", tc.name, run)
		}
	}
}

func TestApplyRunConflict(t *testing.T) {
	c := newRunsFixture(t)
	_, err := c.ApplyRun(t.Context(), "run-locked")
	if err == nil {
		t.Fatal("expected conflict")
	}
	if _, ok := err.(*ConflictError); !ok {
		t.Errorf("expected *ConflictError, got %T: %v", err, err)
	}
}

func TestGetRunPlanJSONFollowsRedirect(t *testing.T) {
	c := newRunsFixture(t)
	// Accepts a run id in any form; server strips the prefix.
	raw, err := c.GetRunPlanJSON(t.Context(), "run-aaaa")
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		FormatVersion string `json:"format_version"`
	}
	if json.Unmarshal(raw, &doc) != nil || doc.FormatVersion != "1.2" {
		t.Errorf("plan json: %s", raw)
	}
}

func TestGetRunPlanJSONNotFound(t *testing.T) {
	c := newRunsFixture(t)
	_, err := c.GetRunPlanJSON(t.Context(), "nooutput")
	if err == nil {
		t.Fatal("expected error")
	}
	if _, ok := err.(*NotFoundError); !ok {
		t.Errorf("expected *NotFoundError, got %T: %v", err, err)
	}
}
