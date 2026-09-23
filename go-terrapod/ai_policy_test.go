package terrapod

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newAIPolicyFixture(t *testing.T) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		p := r.URL.Path
		switch {
		// Nothing recorded → 200 with null data and a reason in meta.
		case r.Method == http.MethodGet && strings.HasSuffix(p, "/runs/run-none/ai-policy"):
			_, _ = w.Write([]byte(`{"data":null,"meta":{"enforcement-level":"off",
			  "blocking":false,"not-evaluated-reason":"The AI policy gate is not enabled for this workspace."}}`))

		// A mandatory deny, with the model's reasons carried verbatim.
		case r.Method == http.MethodGet && strings.HasSuffix(p, "/runs/run-deny/ai-policy"):
			_, _ = w.Write([]byte(`{"data":{"id":"aipol-1","type":"ai-policy-evaluations","attributes":{
			  "enforcement-level":"mandatory","risk-threshold":"off","outcome":"failed",
			  "risk-level":"high","error":null,"overridden-by":null,"overridden-at":"",
			  "created-at":"2026-09-23T10:00:00Z",
			  "verdict":{"decision":"deny","reasons":[
			    {"criterion":"block any 0.0.0.0/0 ingress",
			     "detail":"aws_security_group.web opens port 22 to 0.0.0.0/0"}]}
			}},"meta":{"enforcement-level":"mandatory","blocking":true}}`))

		// An advisory deny: recorded, but never blocking.
		case r.Method == http.MethodGet && strings.HasSuffix(p, "/runs/run-advisory/ai-policy"):
			_, _ = w.Write([]byte(`{"data":{"id":"aipol-2","type":"ai-policy-evaluations","attributes":{
			  "enforcement-level":"advisory","risk-threshold":"high","outcome":"failed",
			  "risk-level":"critical","verdict":{"decision":"deny","reasons":[]}
			}},"meta":{"enforcement-level":"advisory","blocking":false}}`))

		// A budget-exhausted errored verdict -- blocking under a mandatory gate.
		case r.Method == http.MethodGet && strings.HasSuffix(p, "/runs/run-budget/ai-policy"):
			_, _ = w.Write([]byte(`{"data":{"id":"aipol-3","type":"ai-policy-evaluations","attributes":{
			  "enforcement-level":"mandatory","risk-threshold":"off","outcome":"errored",
			  "error":"The deployment's daily AI token budget is exhausted, so no verdict could be reached for this run.",
			  "verdict":{}
			}},"meta":{"enforcement-level":"mandatory","blocking":true}}`))

		case r.Method == http.MethodPost && strings.HasSuffix(p, "/actions/override-ai-policy"):
			_, _ = w.Write([]byte(`{"data":{"id":"aipol-1","type":"ai-policy-evaluations","attributes":{
			  "enforcement-level":"mandatory","risk-threshold":"off","outcome":"failed",
			  "overridden-by":"admin@example.com","overridden-at":"2026-09-23T11:00:00Z",
			  "verdict":{"decision":"deny","reasons":[]}
			}},"meta":{"run-status":"planned"}}`))

		default:
			http.Error(w, `{"errors":[{"status":"404"}]}`, http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestGetRunAIPolicy_Deny(t *testing.T) {
	c := newAIPolicyFixture(t)
	e, err := c.GetRunAIPolicy(t.Context(), "run-deny")
	if err != nil {
		t.Fatalf("GetRunAIPolicy: %v", err)
	}
	if e == nil {
		t.Fatal("expected an evaluation, got nil")
	}
	if e.Outcome != "failed" || e.EnforcementLevel != "mandatory" || e.RiskLevel != "high" {
		t.Fatalf("unexpected evaluation: %+v", e)
	}
	if e.Verdict == nil || e.Verdict.Decision != "deny" {
		t.Fatalf("expected a deny verdict, got %+v", e.Verdict)
	}
	// The reasons are what an operator acts on, so they must survive decoding.
	if len(e.Verdict.Reasons) != 1 {
		t.Fatalf("expected 1 reason, got %d", len(e.Verdict.Reasons))
	}
	if !strings.Contains(e.Verdict.Reasons[0].Detail, "aws_security_group.web") {
		t.Fatalf("reason detail lost the resource address: %q", e.Verdict.Reasons[0].Detail)
	}
	if !e.IsBlocking() {
		t.Fatal("a mandatory, un-overridden deny must block")
	}
}

func TestGetRunAIPolicy_NoneRecordedIsNotAnError(t *testing.T) {
	c := newAIPolicyFixture(t)
	e, err := c.GetRunAIPolicy(t.Context(), "run-none")
	if err != nil {
		t.Fatalf("GetRunAIPolicy: %v", err)
	}
	if e != nil {
		t.Fatalf("expected nil for an unrecorded verdict, got %+v", e)
	}
}

func TestAdvisoryNeverBlocksHoweverBadTheVerdict(t *testing.T) {
	c := newAIPolicyFixture(t)
	e, err := c.GetRunAIPolicy(t.Context(), "run-advisory")
	if err != nil {
		t.Fatalf("GetRunAIPolicy: %v", err)
	}
	if e.Outcome != "failed" {
		t.Fatalf("expected the deny to be recorded, got %q", e.Outcome)
	}
	if e.IsBlocking() {
		t.Fatal("an advisory verdict must never block, whatever its outcome")
	}
}

func TestErroredBlocksUnderAMandatoryGate(t *testing.T) {
	// Fail-closed: a verdict that could not be reached is not consent.
	c := newAIPolicyFixture(t)
	e, err := c.GetRunAIPolicy(t.Context(), "run-budget")
	if err != nil {
		t.Fatalf("GetRunAIPolicy: %v", err)
	}
	if e.Outcome != "errored" {
		t.Fatalf("expected errored, got %q", e.Outcome)
	}
	if !e.IsBlocking() {
		t.Fatal("an errored mandatory verdict must fail closed")
	}
	// The budget case must stay distinguishable from a model fault.
	if !strings.Contains(e.Error, "budget") {
		t.Fatalf("budget exhaustion lost its reason: %q", e.Error)
	}
	// An empty verdict object must not decode into a bogus decision.
	if e.Verdict != nil {
		t.Fatalf("expected no verdict for an errored evaluation, got %+v", e.Verdict)
	}
}

func TestOverrideRunAIPolicyStopsItBlocking(t *testing.T) {
	c := newAIPolicyFixture(t)
	e, err := c.OverrideRunAIPolicy(t.Context(), "run-deny")
	if err != nil {
		t.Fatalf("OverrideRunAIPolicy: %v", err)
	}
	if e.OverriddenBy != "admin@example.com" {
		t.Fatalf("override not recorded: %+v", e)
	}
	if e.IsBlocking() {
		t.Fatal("an overridden verdict must stop blocking")
	}
}

func TestAIPolicyRequiresARunID(t *testing.T) {
	c := newAIPolicyFixture(t)
	if _, err := c.GetRunAIPolicy(t.Context(), ""); err == nil {
		t.Fatal("expected an error for an empty run id")
	}
	if _, err := c.OverrideRunAIPolicy(t.Context(), ""); err == nil {
		t.Fatal("expected an error for an empty run id")
	}
}

func TestNilEvaluationIsNotBlocking(t *testing.T) {
	var e *AIPolicyEvaluation
	if e.IsBlocking() {
		t.Fatal("a nil evaluation must not report as blocking")
	}
}
