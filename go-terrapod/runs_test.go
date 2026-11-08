package terrapod

import (
	"encoding/json"
	"fmt"
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
  "state-diverged":false,"blocked-by":"security-scan",
  "has-cost-estimate":true,"cost-currency":"USD","cost-monthly-min":12.5,"cost-monthly-max":40.0,
  "peak-memory-bytes":536870912,"peak-cpu-usec":null,"runner-exit-code":0,
  "runner-exit-reason":"","workspace-name":"app",
  "agent-pool-id":"apool-11111111-1111-1111-1111-111111111111",
  "candidate-agent-pool-ids":["apool-11111111-1111-1111-1111-111111111111","apool-22222222-2222-2222-2222-222222222222"],
  "vcs-commit-sha":"abc123","vcs-branch":"main","vcs-pull-request-number":null,
  "created-by":"alice@example.com",
  "created-at":"2026-07-17T10:00:00Z","updated-at":"2026-07-17T10:05:00Z",
  "actions":{"is-confirmable":true,"is-discardable":true,"is-cancelable":true,"is-retryable":false},
  "status-timestamps":{"plan-queued-at":"2026-07-17T10:00:00Z","planned-at":"2026-07-17T10:03:00Z"}
}`

func runPayload(id string) string {
	return `{"data":{"id":"` + id + `","type":"runs","attributes":` + runAttrs +
		`,"relationships":{"workspace":{"data":{"id":"ws-app","type":"workspaces"}},"agent-pool":{"data":{"id":"apool-11111111-1111-1111-1111-111111111111","type":"agent-pools"}}}}}`
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
		// Log endpoints: echo back what was asked for so the tests can assert
		// the query the client built, not just that a request happened.
		case r.Method == http.MethodGet && (p == "/api/v2/plans/aaaa/log" ||
			p == "/api/v2/applies/aaaa/log" ||
			// Native, unlike the two above: no CLI consumes a configure, so the
			// route is not on the TFE compatibility surface (#2144). The prefix
			// being different is itself part of what the tests assert.
			p == "/api/v1/configures/aaaa/log"):
			w.Header().Set("Content-Type", "text/plain")
			phase := "plan"
			if strings.HasPrefix(p, "/api/v2/applies/") {
				phase = "apply"
			}
			if strings.HasPrefix(p, "/api/v1/configures/") {
				phase = "configure"
			}
			q := r.URL.Query()
			_, _ = fmt.Fprintf(w, "phase=%s offset=%s limit=%s format=%s",
				phase, q.Get("offset"), q.Get("limit"), q.Get("format"))
			return
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

		// Retry is a Terrapod extension: /api/terrapod/v1, not /api/v2, and it
		// answers with the NEW run.
		case r.Method == http.MethodPost && p == "/api/terrapod/v1/runs/run-nohosts/actions/retry":
			http.Error(w, `{"errors":[{"status":"422","title":"Unprocessable Entity","detail":"run-nohosts has no outstanding hosts, so there is nothing to retry."}]}`, http.StatusUnprocessableEntity)
			return
		case r.Method == http.MethodPost && p == "/api/terrapod/v1/runs/run-busy/actions/retry":
			http.Error(w, `{"errors":[{"status":"409","title":"Conflict","detail":"Cannot retry run in non-terminal state 'planning'"}]}`, http.StatusConflict)
			return
		case r.Method == http.MethodPost && p == "/api/terrapod/v1/runs/run-aaaa/actions/retry":
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(runPayload("run-bbbb")))
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
	// Which pool ran it, and which could have (#1231).
	if run.AgentPoolID != "apool-11111111-1111-1111-1111-111111111111" {
		t.Errorf("agent-pool-id: %q", run.AgentPoolID)
	}
	if len(run.CandidateAgentPoolIDs) != 2 ||
		run.CandidateAgentPoolIDs[0] != "apool-11111111-1111-1111-1111-111111111111" {
		t.Errorf("candidate-agent-pool-ids: %+v", run.CandidateAgentPoolIDs)
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
	// A run held at a post-plan gate says which gate (#1725).
	if run.BlockedBy != "security-scan" {
		t.Errorf("blocked-by: %q", run.BlockedBy)
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

// ── The run queue (#2425 §4.4) ───────────────────────────────────────────────
//
// Wire tests, because §4.3 of the same issue is about SDK methods that shipped
// without them -- `CreateRunRequest.Variables` does a non-trivial transform and
// had none, while its sibling `ConfigureAfterApply` got a thorough one in the
// same commit. A method added in the PR that records that is not going out the
// same way.

func newQueueFixture(t *testing.T) (*Client, *struct{ path string }) {
	t.Helper()
	got := &struct{ path string }{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got.path = r.URL.RequestURI()
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch r.URL.Path {
		case "/api/v1/run-queue":
			// Two runs, positions 1 and 3 -- NOT 1 and 2. The gap is the point:
			// positions are counted over the whole queue while the listing is
			// filtered by reach, so a consumer must not renumber what it reads
			// or infer the queue's length from the slice it was given.
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"run-aaaa","type":"runs","attributes":{
			     "status":"queued","position-in-queue":1},
			   "relationships":{"workspace":{"data":{"id":"ws-app","type":"workspaces"}}}},
			  {"id":"run-bbbb","type":"runs","attributes":{
			     "status":"confirmed","position-in-queue":3},
			   "relationships":{"workspace":{"data":{"id":"ws-other","type":"workspaces"}}}}],
			  "meta":{"pagination":{"current-page":1,"page-size":20,"total-count":2,"total-pages":1}}}`))
		case "/api/v1/run-capacity":
			_, _ = w.Write([]byte(`{"data":{"id":"default","type":"run-capacity",
			  "attributes":{"pending":4,"running":2}}}`))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c, got
}

func TestListRunQueue(t *testing.T) {
	c, got := newQueueFixture(t)
	queue, err := c.ListRunQueue(t.Context(), 0, 0)
	if err != nil {
		t.Fatal(err)
	}
	// The NATIVE path. Until #2425 the queue was reachable only at
	// `/api/tfe/v2/organizations/default/runs/queue`, and the whole point of
	// the method is that it is not that.
	if got.path != "/api/v1/run-queue" {
		t.Errorf("path = %q, want /api/v1/run-queue", got.path)
	}
	if len(queue) != 2 {
		t.Fatalf("queue = %+v, want 2 entries", queue)
	}
	// The embedded Run is parsed, not just the position: a QueuedRun that
	// carried a position and an empty Run would satisfy a position-only
	// assertion while being useless.
	if queue[0].ID != "run-aaaa" || queue[0].Status != "queued" ||
		queue[0].WorkspaceID != "ws-app" {
		t.Errorf("queue[0] = %+v, want the run parsed too", queue[0])
	}
	if queue[0].PositionInQueue != 1 || queue[1].PositionInQueue != 3 {
		t.Errorf("positions = %d, %d; want 1 and 3 -- the gap is deliberate",
			queue[0].PositionInQueue, queue[1].PositionInQueue)
	}
}

func TestListRunQueuePagesWhenAsked(t *testing.T) {
	c, got := newQueueFixture(t)
	if _, err := c.ListRunQueue(t.Context(), 2, 50); err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"page%5Bnumber%5D=2", "page%5Bsize%5D=50"} {
		if !strings.Contains(got.path, want) {
			t.Errorf("path %q is missing %s", got.path, want)
		}
	}
}

func TestListRunQueueSendsNoQueryByDefault(t *testing.T) {
	// The whole queue, with no page parameters -- the contrast that makes the
	// test above about PAGING rather than about the path having a query string.
	c, got := newQueueFixture(t)
	if _, err := c.ListRunQueue(t.Context(), 0, 0); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(got.path, "?") {
		t.Errorf("path = %q, want no query string", got.path)
	}
}

func TestGetRunCapacity(t *testing.T) {
	c, got := newQueueFixture(t)
	capacity, err := c.GetRunCapacity(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if got.path != "/api/v1/run-capacity" {
		t.Errorf("path = %q, want /api/v1/run-capacity", got.path)
	}
	// Ints, not strings: `position - running` is arithmetic, and a consumer
	// doing it on a string gets a compile error at best and a concatenation at
	// worst in a language that allows it.
	if capacity.Pending != 4 || capacity.Running != 2 {
		t.Errorf("capacity = %+v, want pending 4 running 2", capacity)
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

func TestRetryRunReturnsTheNewRun(t *testing.T) {
	c := newRunsFixture(t)
	for _, id := range []string{"run-aaaa", "aaaa"} {
		run, err := c.RetryRun(t.Context(), id)
		if err != nil {
			t.Fatalf("%s: %v", id, err)
		}
		// The retry is a new run, not the one it was retried from.
		if run.ID != "run-bbbb" {
			t.Errorf("%s: got %q, want the new run run-bbbb", id, run.ID)
		}
	}
}

func TestRetryRunOnAnUnfinishedRunIsAConflict(t *testing.T) {
	c := newRunsFixture(t)
	_, err := c.RetryRun(t.Context(), "run-busy")
	if err == nil {
		t.Fatal("expected a conflict")
	}
	if _, ok := err.(*ConflictError); !ok {
		t.Errorf("expected *ConflictError, got %T: %v", err, err)
	}
}

func TestRetryRunNeedsAnID(t *testing.T) {
	c := newRunsFixture(t)
	if _, err := c.RetryRun(t.Context(), ""); err == nil {
		t.Fatal("expected an error for an empty run id")
	}
}

func TestRetryRunOutstandingHostsSendsTheFlagAndReturnsTheNewRun(t *testing.T) {
	// Its own server rather than the shared fixture, because the property under
	// test is the BODY on the wire. The shared fixture answers this path for
	// RetryRun too, so a test written against it would pass whether or not the
	// flag was sent -- and the flag is the whole difference between retrying
	// two hosts and retrying the entire inventory.
	var body []byte
	var gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/.well-known/") {
			_, _ = w.Write([]byte(`{"tfe.v2":"/api/v2/"}`))
			return
		}
		gotPath = r.URL.Path
		body, _ = io.ReadAll(r.Body)
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(runPayload("run-bbbb")))
	}))
	t.Cleanup(srv.Close)

	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	run, err := c.RetryRunOutstandingHosts(t.Context(), "run-aaaa")
	if err != nil {
		t.Fatal(err)
	}
	if run.ID != "run-bbbb" {
		t.Errorf("got %q, want the new run run-bbbb", run.ID)
	}
	if gotPath != "/api/terrapod/v1/runs/run-aaaa/actions/retry" {
		t.Errorf("posted to %q", gotPath)
	}
	var sent map[string]any
	if err := json.Unmarshal(body, &sent); err != nil {
		t.Fatalf("body %q is not JSON: %v", body, err)
	}
	// A literal true. The server accepts nothing else -- a truthy string there
	// means a FULL retry, which on a partial configure is every host in the
	// inventory rather than the handful that are left.
	if sent["only-outstanding-hosts"] != true {
		t.Errorf("body was %q; want only-outstanding-hosts set to true", body)
	}
}

func TestRetryRunOutstandingHostsWithNothingOutstandingIsAValidationError(t *testing.T) {
	// A refusal, not a no-op: an empty limit means the workspace's own default,
	// so a server that fell through here would re-run the whole inventory.
	c := newRunsFixture(t)
	_, err := c.RetryRunOutstandingHosts(t.Context(), "run-nohosts")
	if err == nil {
		t.Fatal("expected a validation error")
	}
	if _, ok := err.(*ValidationError); !ok {
		t.Errorf("expected *ValidationError, got %T: %v", err, err)
	}
}

func TestRetryRunOutstandingHostsNeedsAnID(t *testing.T) {
	c := newRunsFixture(t)
	if _, err := c.RetryRunOutstandingHosts(t.Context(), ""); err == nil {
		t.Fatal("expected an error for an empty run id")
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

func TestGetPlanLogAcceptsAnyIDForm(t *testing.T) {
	c := newRunsFixture(t)
	// Plan, apply and bare forms all key on the same run UUID.
	for _, id := range []string{"aaaa", "run-aaaa", "plan-aaaa"} {
		got, err := c.GetPlanLog(t.Context(), id, nil)
		if err != nil {
			t.Fatalf("%s: %v", id, err)
		}
		if !strings.Contains(string(got), "phase=plan") {
			t.Errorf("%s: got %s", id, got)
		}
	}
}

func TestGetApplyLogHitsTheAppliesCollection(t *testing.T) {
	c := newRunsFixture(t)
	got, err := c.GetApplyLog(t.Context(), "apply-aaaa", nil)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(got), "phase=apply") {
		t.Errorf("got %s", got)
	}
}

func TestGetConfigureLogHitsTheNativeConfiguresCollection(t *testing.T) {
	c := newRunsFixture(t)
	// Every id form, including its own prefix: a configure id is the run's UUID
	// with `configure-` on it, the same shape as the other two phases.
	for _, id := range []string{"aaaa", "run-aaaa", "configure-aaaa"} {
		got, err := c.GetConfigureLog(t.Context(), id, nil)
		if err != nil {
			t.Fatalf("%s: %v", id, err)
		}
		if !strings.Contains(string(got), "phase=configure") {
			t.Errorf("%s: got %s", id, got)
		}
	}
}

func TestGetConfigureLogIsNotOnTheCompatibilitySurface(t *testing.T) {
	// The fixture serves the configure log at `/api/v1/` ALONE, so a client
	// that reached for `/api/v2/` would 404 here rather than silently working.
	// That is the assertion: mounting a native concept on the surface the CLI
	// reads is the #1905 mistake in reverse.
	var seen string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r.URL.Path
		w.Header().Set("Content-Type", "text/plain")
		_, _ = w.Write([]byte("ok"))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.GetConfigureLog(t.Context(), "run-aaaa", nil); err != nil {
		t.Fatal(err)
	}
	if seen != "/api/v1/configures/aaaa/log" {
		t.Errorf("configure log went to %q, expected the native prefix", seen)
	}
}

func TestGetConfigureLogRequiresARunID(t *testing.T) {
	c := newRunsFixture(t)
	if _, err := c.GetConfigureLog(t.Context(), "configure-", nil); err == nil {
		t.Error("an id that is only a prefix must be refused, not sent as an empty path")
	}
}

func TestGetConfigureLogPassesPlainThrough(t *testing.T) {
	// `Plain` is what strips the per-host prefix as well as the ANSI codes, so a
	// consumer that wants ansible's own words has one flag rather than a parser.
	c := newRunsFixture(t)
	got, err := c.GetConfigureLog(t.Context(), "run-aaaa", &LogOptions{Plain: true})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(got), "format=plain") {
		t.Errorf("got %s", got)
	}
}

func TestLogOptionsBecomeQueryParameters(t *testing.T) {
	c := newRunsFixture(t)
	got, err := c.GetPlanLog(t.Context(), "run-aaaa", &LogOptions{Offset: 4096, Limit: 16384, Plain: true})
	if err != nil {
		t.Fatal(err)
	}
	want := "offset=4096 limit=16384 format=plain"
	if !strings.Contains(string(got), want) {
		t.Errorf("want %q in %q", want, got)
	}
}

func TestNilLogOptionsSendsNoQuery(t *testing.T) {
	// The zero read must not pin defaults the server owns: sending
	// offset=0&limit=0 would look deliberate and could mean "return nothing"
	// to a stricter handler.
	c := newRunsFixture(t)
	got, err := c.GetPlanLog(t.Context(), "run-aaaa", nil)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(got), "offset= limit= format=") {
		t.Errorf("expected an unqualified request, got %s", got)
	}
}

func TestRunLogRejectsAnEmptyID(t *testing.T) {
	c := newRunsFixture(t)
	if _, err := c.GetPlanLog(t.Context(), "run-", nil); err == nil {
		t.Fatal("expected an error for an id that is only a prefix")
	}
}

// A standalone configure is asked for with one attribute, and the plan/apply
// options are NOT sent with it (#1974): the server refuses them on a configure,
// so an SDK that sent a zero-valued `is-destroy: false` alongside would be
// relying on the server reading false as absent.
func TestCreateRunAsksForAConfigure(t *testing.T) {
	var body map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&body)
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"data":{"id":"run-cfg","type":"runs","attributes":{
			"status":"queued","is-configure":true,"configure-after-apply":false,
			"playbook":"patch.yml","limit":"canary"}}}`))
	}))
	defer srv.Close()

	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	run, err := c.CreateRun(t.Context(), CreateRunRequest{
		WorkspaceID: "ws-app",
		IsConfigure: true,
		Playbook:    "patch.yml",
		Limit:       "canary",
	})
	if err != nil {
		t.Fatal(err)
	}
	attrs, _ := body["data"].(map[string]any)["attributes"].(map[string]any)
	if attrs["is-configure"] != true {
		t.Errorf("is-configure not sent: %v", attrs)
	}
	if attrs["playbook"] != "patch.yml" || attrs["limit"] != "canary" {
		t.Errorf("playbook/limit not sent: %v", attrs)
	}
	for _, unwanted := range []string{"is-destroy", "refresh-only", "save-plan", "allow-empty-apply"} {
		if _, present := attrs[unwanted]; present {
			t.Errorf("%s was sent on a configure request: %v", unwanted, attrs)
		}
	}
	// And the read-back carries the shape, so a caller can tell a standalone
	// configure from a composite without asking the workspace.
	if !run.IsConfigure || run.ConfigureAfterApply {
		t.Errorf("shape: is-configure=%v after-apply=%v", run.IsConfigure, run.ConfigureAfterApply)
	}
	if run.Playbook != "patch.yml" || run.Limit != "canary" {
		t.Errorf("options: %q %q", run.Playbook, run.Limit)
	}
}

// The three states of ConfigureAfterApply (#2413 F12), each asserted on the
// WIRE rather than on the struct.
//
// The absent case is the one that matters and the one a boolean field could not
// express: the server distinguishes an absent key from a false one, so an SDK
// that emitted `configure-after-apply: false` for an unset field would silently
// stop every configure-after-apply workspace producing a composite. That is a
// behaviour change for runs nobody asked to change, delivered by a client
// upgrade, which is the worst shape a default can have.
func TestCreateRunConfigureAfterApplyIsThreeState(t *testing.T) {
	yes, no := true, false
	for _, tc := range []struct {
		name  string
		field *bool
		want  any // nil means the key must be ABSENT
	}{
		{"nil sends nothing", nil, nil},
		{"true asks for a composite", &yes, true},
		{"false declines it", &no, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var body map[string]any
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				_ = json.NewDecoder(r.Body).Decode(&body)
				w.Header().Set("Content-Type", "application/vnd.api+json")
				w.WriteHeader(http.StatusCreated)
				_, _ = w.Write([]byte(`{"data":{"id":"run-c","type":"runs","attributes":{"status":"queued"}}}`))
			}))
			defer srv.Close()

			c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
			if err != nil {
				t.Fatal(err)
			}
			if _, err := c.CreateRun(t.Context(), CreateRunRequest{
				WorkspaceID:         "ws-app",
				ConfigureAfterApply: tc.field,
			}); err != nil {
				t.Fatal(err)
			}
			attrs, _ := body["data"].(map[string]any)["attributes"].(map[string]any)
			got, present := attrs["configure-after-apply"]
			if tc.want == nil {
				if present {
					t.Errorf("key sent for a nil field: %v -- the workspace setting would be overridden", attrs)
				}
				return
			}
			if !present {
				t.Fatalf("key not sent: %v", attrs)
			}
			if got != tc.want {
				t.Errorf("configure-after-apply = %v, want %v", got, tc.want)
			}
		})
	}
}

// ── Run variables (#2425 §4.3) ───────────────────────────────────────────────
//
// `CreateRunRequest.Variables` is a map in Go and a sorted list of objects on
// the wire, and `Run.RunVariableKeys` is the read side. Both shipped without a
// wire test while their sibling `ConfigureAfterApply` got a thorough one in the
// same commit, which is the gap §4.3 records.
//
// A transform between two shapes is exactly where a test earns its keep: the
// struct round-trips through itself whatever the transform does, so only the
// body the server receives can say whether it is right.

// bodyCapturingRunServer returns a client and a pointer to the raw bytes of the
// last request body -- RAW, not a decoded map, because one of the properties
// under test is about byte stability and a map cannot express ordering.
func bodyCapturingRunServer(t *testing.T) (*Client, *[]byte) {
	t.Helper()
	var raw []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ = io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"data":{"id":"run-v","type":"runs","attributes":{"status":"queued"}}}`))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c, &raw
}

// variablesOnTheWire pulls the `variables` attribute out of a captured body,
// reporting separately whether the key was present at all -- the absent case is
// a distinct assertion and `nil, true` must not read as `nil, false`.
func variablesOnTheWire(t *testing.T, raw []byte) ([]map[string]string, bool) {
	t.Helper()
	var body struct {
		Data struct {
			Attributes map[string]json.RawMessage `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(raw, &body); err != nil {
		t.Fatalf("undecodable body %q: %v", raw, err)
	}
	enc, present := body.Data.Attributes["variables"]
	if !present {
		return nil, false
	}
	var out []map[string]string
	if err := json.Unmarshal(enc, &out); err != nil {
		t.Fatalf("variables is not a list of objects: %q: %v", enc, err)
	}
	return out, true
}

// Sorted by KEY, and the same request twice is the same bytes.
//
// Both halves are needed and neither implies the other. Go randomises map
// iteration, so an unsorted implementation produces a different permutation per
// call: the byte-stability half is what catches it, and with five keys a run of
// identical bodies is not luck. The sorted-order half is what catches a sort
// that is descending, or by value, which would be perfectly stable and wrong.
func TestCreateRunVariablesAreSortedAndByteStable(t *testing.T) {
	c, raw := bodyCapturingRunServer(t)
	// Inserted in scrambled order, and deliberately NOT in an order a stable
	// pass-through would sort into by accident.
	req := CreateRunRequest{
		WorkspaceID: "ws-app",
		Variables: map[string]string{
			"region": "eu-west-1",
			"az":     "a",
			"zone":   "z",
			"count":  "3",
			"name":   "app",
		},
	}
	wantKeys := []string{"az", "count", "name", "region", "zone"}

	var first []byte
	for i := range 20 {
		if _, err := c.CreateRun(t.Context(), req); err != nil {
			t.Fatal(err)
		}
		if i == 0 {
			first = append([]byte(nil), *raw...)
			continue
		}
		if string(*raw) != string(first) {
			t.Fatalf("call %d sent different bytes:\n first: %s\n  this: %s", i, first, *raw)
		}
	}

	vars, present := variablesOnTheWire(t, first)
	if !present {
		t.Fatal("variables was not sent at all")
	}
	if len(vars) != len(wantKeys) {
		t.Fatalf("sent %d entries, want %d: %+v", len(vars), len(wantKeys), vars)
	}
	for i, want := range wantKeys {
		if vars[i]["key"] != want {
			t.Errorf("entry %d key = %q, want %q (order: %+v)", i, vars[i]["key"], want, vars)
		}
		if vars[i]["value"] != req.Variables[want] {
			t.Errorf("entry %d value = %q, want %q", i, vars[i]["value"], req.Variables[want])
		}
	}
	// The entry carries exactly the two keys the server reads. An extra key
	// would be accepted and ignored today and is a wire claim nobody made.
	for i, e := range vars {
		if len(e) != 2 {
			t.Errorf("entry %d = %+v, want exactly key+value", i, e)
		}
	}
}

// Absent, not empty -- and the distinction is the server's, not a style choice:
// it treats an empty array as a no-op, so sending one would work and would rely
// on that. Sending nothing says the same thing without the dependency.
func TestCreateRunVariablesAreOmittedWhenThereAreNone(t *testing.T) {
	for _, tc := range []struct {
		name string
		vars map[string]string
	}{
		{"nil map", nil},
		{"empty map", map[string]string{}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c, raw := bodyCapturingRunServer(t)
			if _, err := c.CreateRun(t.Context(), CreateRunRequest{
				WorkspaceID: "ws-app",
				Variables:   tc.vars,
			}); err != nil {
				t.Fatal(err)
			}
			if _, present := variablesOnTheWire(t, *raw); present {
				t.Errorf("variables was sent for %s: %s", tc.name, *raw)
			}
		})
	}
}

// A value is sent verbatim, including an EMPTY one.
//
// The empty-string case is the one worth pinning: the omission above is decided
// by the number of entries, not by their contents, so `{"msg": ""}` is a
// variable the operator set to the empty string and must arrive. A reader who
// assumed the emptiness check went one level deeper would drop it, and the run
// would plan against whatever the workspace variable said instead -- which is
// the dropped-`-var` defect this field exists to fix, by a new door.
func TestCreateRunVariableValuesAreSentVerbatim(t *testing.T) {
	c, raw := bodyCapturingRunServer(t)
	in := map[string]string{
		"empty":   "",
		"quoted":  `a "b" c`,
		"newline": "one\ntwo",
		"unicode": "région-é",
		"jsonish": `{"not":"parsed"}`,
	}
	if _, err := c.CreateRun(t.Context(), CreateRunRequest{
		WorkspaceID: "ws-app",
		Variables:   in,
	}); err != nil {
		t.Fatal(err)
	}
	vars, present := variablesOnTheWire(t, *raw)
	if !present {
		t.Fatal("variables was not sent")
	}
	if len(vars) != len(in) {
		t.Fatalf("sent %d entries, want %d: %+v", len(vars), len(in), vars)
	}
	for _, e := range vars {
		if want := in[e["key"]]; e["value"] != want {
			t.Errorf("%q = %q, want %q", e["key"], e["value"], want)
		}
	}
}

// The read side: `run-variable-keys` parses, and its absence is nil rather than
// a panic or a one-element slice holding "".
//
// The server emits the KEYS and never the values, deliberately -- a run
// variable may be a secret and `RunVariable` carries no sensitive flag, so
// there is nothing to decide which to withhold. A consumer reading this field
// learns which variables were overridden and cannot learn what they were, and
// the test says so because the obvious next request is "and the values".
func TestRunParsesRunVariableKeys(t *testing.T) {
	for _, tc := range []struct {
		name  string
		attrs string
		want  []string
	}{
		{"present", `{"status":"planned","run-variable-keys":["region","name"]}`, []string{"region", "name"}},
		{"absent", `{"status":"planned"}`, nil},
		{"null", `{"status":"planned","run-variable-keys":null}`, nil},
		{"empty list", `{"status":"planned","run-variable-keys":[]}`, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.Header().Set("Content-Type", "application/vnd.api+json")
				_, _ = fmt.Fprintf(w, `{"data":{"id":"run-k","type":"runs","attributes":%s}}`, tc.attrs)
			}))
			defer srv.Close()
			c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
			if err != nil {
				t.Fatal(err)
			}
			run, err := c.GetRun(t.Context(), "run-k")
			if err != nil {
				t.Fatal(err)
			}
			if len(run.RunVariableKeys) != len(tc.want) {
				t.Fatalf("keys = %#v, want %#v", run.RunVariableKeys, tc.want)
			}
			for i, want := range tc.want {
				if run.RunVariableKeys[i] != want {
					t.Errorf("keys[%d] = %q, want %q", i, run.RunVariableKeys[i], want)
				}
			}
			// Order is the server's, so it is asserted as given above rather
			// than sorted here: the API emits the keys in the order the run
			// recorded them, and a consumer re-sorting is a consumer's choice.
		})
	}
}
