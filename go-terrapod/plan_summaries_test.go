package terrapod

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newPlanSummaryFixture(t *testing.T) *Client {
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
		// GET /api/v2/plans/{id}/summary — the fixture accepts the
		// prefixed "plan-<uuid>" form (the SDK adds the prefix).
		case r.Method == http.MethodGet && p == "/api/v2/plans/plan-missing/summary":
			http.Error(w, `{"errors":[{"status":"404","title":"Not Found","detail":"No summary for this plan"}]}`, http.StatusNotFound)
		case r.Method == http.MethodGet && strings.HasPrefix(p, "/api/v2/plans/plan-") && strings.HasSuffix(p, "/summary"):
			_, _ = w.Write([]byte(`{"data":{"id":"plan-11111111-1111-1111-1111-111111111111",
			  "type":"plan-summaries","attributes":{
			  "kind":"plan_summary","status":"ready",
			  "description":"Adds a VPC and two subnets.",
			  "risk-level":"medium",
			  "risk-factors":[
			    {"severity":"high","title":"IGW replacement","detail":"The internet gateway is replaced.","resource_address":"aws_internet_gateway.this"},
			    {"severity":"low","title":"Tag drift","detail":"Tags updated in place."}
			  ],
			  "model":"claude","input-tokens":1200,"output-tokens":340,
			  "created-at":"2026-07-17T10:00:00Z","updated-at":"2026-07-17T10:01:00Z"
			  },"relationships":{
			    "run":{"data":{"id":"run-22222222-2222-2222-2222-222222222222","type":"runs"}}
			  }}}`))

		// POST /api/terrapod/v1/runs/{id}/plan-summary/regenerate
		case r.Method == http.MethodPost && p == "/api/terrapod/v1/runs/run-conflict/plan-summary/regenerate":
			http.Error(w, `{"errors":[{"status":"409","title":"Conflict","detail":"Run has no summarisable output yet"}]}`, http.StatusConflict)
		case r.Method == http.MethodPost && strings.HasSuffix(p, "/plan-summary/regenerate"):
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"plan-11111111-1111-1111-1111-111111111111",
			  "type":"plan-summaries","attributes":{"kind":"plan_summary","status":"pending"},
			  "relationships":{"run":{"data":{"id":"run-22222222-2222-2222-2222-222222222222","type":"runs"}}}}}`))

		// POST /api/terrapod/v1/runs/{id}/plan-summary/messages
		case r.Method == http.MethodPost && p == "/api/terrapod/v1/runs/run-msgconflict/plan-summary/messages":
			http.Error(w, `{"errors":[{"status":"409","title":"Conflict","detail":"Initial summary not ready"}]}`, http.StatusConflict)
		case r.Method == http.MethodPost && strings.HasSuffix(p, "/plan-summary/messages"):
			// Assert the client forwarded the content attribute.
			var doc struct {
				Data struct {
					Attributes map[string]any `json:"attributes"`
				} `json:"data"`
			}
			_ = json.Unmarshal(reqBody, &doc)
			if doc.Data.Attributes["content"] != "why is the IGW replaced?" {
				http.Error(w, "missing content", http.StatusUnprocessableEntity)
				return
			}
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"msg-2","type":"plan-summary-messages","attributes":{
			  "role":"assistant","content":"Because its attached VPC changed.","model":"claude",
			  "input-tokens":80,"output-tokens":25,"created-at":"2026-07-17T10:05:00Z"}}}`))

		// GET /api/terrapod/v1/runs/{id}/plan-summary/messages
		case r.Method == http.MethodGet && p == "/api/terrapod/v1/runs/run-empty/plan-summary/messages":
			_, _ = w.Write([]byte(`{"data":[]}`))
		case r.Method == http.MethodGet && strings.HasSuffix(p, "/plan-summary/messages"):
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"msg-1","type":"plan-summary-messages","attributes":{"role":"user","content":"why is the IGW replaced?"}},
			  {"id":"msg-2","type":"plan-summary-messages","attributes":{"role":"assistant","content":"Because its attached VPC changed."}}
			]}`))

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

func TestGetPlanSummary(t *testing.T) {
	c := newPlanSummaryFixture(t)
	// Pass a bare UUID; the SDK must prefix it with "plan-".
	s, err := c.GetPlanSummary(t.Context(), "11111111-1111-1111-1111-111111111111")
	if err != nil {
		t.Fatal(err)
	}
	if s.Kind != "plan_summary" || s.Status != "ready" {
		t.Errorf("kind/status: %+v", s)
	}
	if s.Description != "Adds a VPC and two subnets." || s.RiskLevel != "medium" {
		t.Errorf("description/risk-level: %+v", s)
	}
	if s.InputTokens != 1200 || s.OutputTokens != 340 {
		t.Errorf("token counts: %+v", s)
	}
	// run relationship → resolved into RunID (verbatim, prefix retained).
	if s.RunID != "run-22222222-2222-2222-2222-222222222222" {
		t.Errorf("run id: %q", s.RunID)
	}
	if len(s.RiskFactors) != 2 {
		t.Fatalf("risk-factors: %+v", s.RiskFactors)
	}
	rf := s.RiskFactors[0]
	if rf.Severity != "high" || rf.Title != "IGW replacement" || rf.ResourceAddress != "aws_internet_gateway.this" {
		t.Errorf("risk factor 0: %+v", rf)
	}
	if s.RiskFactors[1].ResourceAddress != "" {
		t.Errorf("risk factor 1 should have empty address: %+v", s.RiskFactors[1])
	}
}

func TestGetPlanSummaryNotFound(t *testing.T) {
	c := newPlanSummaryFixture(t)
	_, err := c.GetPlanSummary(t.Context(), "missing")
	if err == nil {
		t.Fatal("expected error")
	}
	if _, ok := err.(*NotFoundError); !ok {
		t.Errorf("expected *NotFoundError, got %T: %v", err, err)
	}
}

func TestGetPlanSummaryRequiresID(t *testing.T) {
	c := newPlanSummaryFixture(t)
	if _, err := c.GetPlanSummary(t.Context(), ""); err == nil {
		t.Fatal("expected error for empty plan id")
	}
}

func TestRegeneratePlanSummary(t *testing.T) {
	c := newPlanSummaryFixture(t)
	s, err := c.RegeneratePlanSummary(t.Context(), "22222222-2222-2222-2222-222222222222")
	if err != nil {
		t.Fatal(err)
	}
	if s.Status != "pending" || s.Kind != "plan_summary" {
		t.Errorf("regenerated: %+v", s)
	}
	if s.RunID != "run-22222222-2222-2222-2222-222222222222" {
		t.Errorf("run id: %q", s.RunID)
	}
}

func TestRegeneratePlanSummaryConflict(t *testing.T) {
	c := newPlanSummaryFixture(t)
	_, err := c.RegeneratePlanSummary(t.Context(), "conflict")
	if err == nil {
		t.Fatal("expected conflict error")
	}
	if _, ok := err.(*ConflictError); !ok {
		t.Errorf("expected *ConflictError, got %T: %v", err, err)
	}
}

func TestListPlanSummaryMessages(t *testing.T) {
	c := newPlanSummaryFixture(t)
	msgs, err := c.ListPlanSummaryMessages(t.Context(), "22222222-2222-2222-2222-222222222222")
	if err != nil {
		t.Fatal(err)
	}
	if len(msgs) != 2 {
		t.Fatalf("messages: %+v", msgs)
	}
	if msgs[0].Role != "user" || msgs[0].Content != "why is the IGW replaced?" {
		t.Errorf("msg 0: %+v", msgs[0])
	}
	if msgs[1].Role != "assistant" {
		t.Errorf("msg 1: %+v", msgs[1])
	}
}

func TestListPlanSummaryMessagesEmpty(t *testing.T) {
	c := newPlanSummaryFixture(t)
	msgs, err := c.ListPlanSummaryMessages(t.Context(), "empty")
	if err != nil {
		t.Fatal(err)
	}
	if len(msgs) != 0 {
		t.Errorf("expected empty, got %+v", msgs)
	}
}

func TestPostPlanSummaryMessage(t *testing.T) {
	c := newPlanSummaryFixture(t)
	reply, err := c.PostPlanSummaryMessage(t.Context(),
		"22222222-2222-2222-2222-222222222222", "why is the IGW replaced?")
	if err != nil {
		t.Fatal(err)
	}
	if reply.Role != "assistant" || reply.Content != "Because its attached VPC changed." {
		t.Errorf("reply: %+v", reply)
	}
	if reply.OutputTokens != 25 {
		t.Errorf("output tokens: %+v", reply)
	}
}

func TestPostPlanSummaryMessageEmptyContentGuard(t *testing.T) {
	c := newPlanSummaryFixture(t)
	// Empty content must be rejected client-side without hitting the server.
	if _, err := c.PostPlanSummaryMessage(t.Context(), "run-x", ""); err == nil {
		t.Fatal("expected error for empty content")
	}
	// Empty run id likewise.
	if _, err := c.PostPlanSummaryMessage(t.Context(), "", "hello"); err == nil {
		t.Fatal("expected error for empty run id")
	}
}

func TestPostPlanSummaryMessageConflict(t *testing.T) {
	c := newPlanSummaryFixture(t)
	_, err := c.PostPlanSummaryMessage(t.Context(), "msgconflict", "why is the IGW replaced?")
	if err == nil {
		t.Fatal("expected conflict error")
	}
	if _, ok := err.(*ConflictError); !ok {
		t.Errorf("expected *ConflictError, got %T: %v", err, err)
	}
}
