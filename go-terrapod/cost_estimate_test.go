package terrapod

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func newCostEstimateFixture(t *testing.T, status int, body string) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		if status != http.StatusOK {
			w.WriteHeader(status)
			_, _ = w.Write([]byte(`{"errors":[{"detail":"no cost estimate for this run"}]}`))
			return
		}
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

const costEstimateBody = `{"data":{"id":"cost-estimate-abc","type":"cost-estimates",
  "attributes":{
    "currency":"USD",
    "total":{"min":219.0,"max":219.0},
    "previous":{"min":73.0,"max":73.0},
    "diff":{"min":146.0,"max":146.0},
    "resources":[
      {"address":"aws_instance.eu","type":"aws_instance","name":"eu","change":"add","monthly":{"min":146.0,"max":146.0}},
      {"address":"aws_instance.us","type":"aws_instance","name":"us","change":"noop","monthly":{"min":73.0,"max":73.0},
       "usage_assumptions":[{"description":"NAT data","dimension":"data processed","unit":"GB/month","low":10,"typical":100,"high":50000,"cost_low":0.45,"cost_typical":4.5,"cost_high":2250.0}]}
    ],
    "unpriced":[
      {"address":"random_pet.name","type":"random_pet","change":"add"}
    ]
  },
  "relationships":{"run":{"data":{"id":"run-abc","type":"runs"}}}}}`

func TestGetRunCostEstimate(t *testing.T) {
	c := newCostEstimateFixture(t, http.StatusOK, costEstimateBody)
	e, err := c.GetRunCostEstimate(t.Context(), "abc") // bare UUID → "run-abc"
	if err != nil {
		t.Fatal(err)
	}
	if e.RunID != "abc" {
		t.Errorf("RunID = %q, want abc (prefix stripped)", e.RunID)
	}
	if e.Currency != "USD" {
		t.Errorf("Currency = %q, want USD", e.Currency)
	}
	if e.Total.Min != 219.0 || e.Diff.Max != 146.0 || e.Previous.Min != 73.0 {
		t.Errorf("ranges not decoded: total=%+v diff=%+v prev=%+v", e.Total, e.Diff, e.Previous)
	}
	if len(e.Resources) != 2 || len(e.Unpriced) != 1 {
		t.Fatalf("resources=%d unpriced=%d, want 2/1", len(e.Resources), len(e.Unpriced))
	}
	if len(e.Resources[0].UsageAssumptions) != 0 {
		t.Errorf("deterministic resource should have no usage assumptions, got %+v", e.Resources[0].UsageAssumptions)
	}
	if ua := e.Resources[1].UsageAssumptions; len(ua) != 1 || ua[0].Dimension != "data processed" ||
		ua[0].Typical != 100 || ua[0].High != 50000 {
		t.Errorf("usage_assumptions not decoded: %+v", e.Resources[1].UsageAssumptions)
	}
	if ct := e.Resources[1].UsageAssumptions[0].CostTypical; ct == nil || *ct != 4.5 {
		t.Errorf("cost_typical not decoded: %v", ct)
	}
	if ch := e.Resources[1].UsageAssumptions[0].CostHigh; ch == nil || *ch != 2250.0 {
		t.Errorf("cost_high not decoded: %v", ch)
	}
	// Deterministic resource has no cost band pointers set either.
	if len(e.Resources[0].UsageAssumptions) != 0 {
		t.Errorf("resource[0] should carry no assumptions")
	}
	if e.Resources[0].Change != "add" || e.Resources[0].Monthly.Min != 146.0 {
		t.Errorf("resource not decoded: %+v", e.Resources[0])
	}
	if e.Unpriced[0].Address != "random_pet.name" {
		t.Errorf("unpriced not decoded: %+v", e.Unpriced[0])
	}
}

func TestGetRunCostEstimateNotFound(t *testing.T) {
	c := newCostEstimateFixture(t, http.StatusNotFound, "")
	_, err := c.GetRunCostEstimate(t.Context(), "run-none")
	if !IsNotFound(err) {
		t.Fatalf("want NotFoundError, got %v", err)
	}
}

func TestGetRunCostEstimateEmptyID(t *testing.T) {
	c := newCostEstimateFixture(t, http.StatusOK, costEstimateBody)
	if _, err := c.GetRunCostEstimate(t.Context(), ""); err == nil {
		t.Fatal("want error for empty run id")
	}
}

const workspaceCostBody = `{"data":{"id":"workspace-cost-ws1","type":"workspace-cost-estimates",
  "attributes":{
    "currency":"USD",
    "total":{"min":292.0,"max":292.0},
    "previous":{"min":292.0,"max":292.0},
    "diff":{"min":0.0,"max":0.0},
    "resources":[
      {"address":"aws_instance.web","type":"aws_instance","name":"web","change":"noop","monthly":{"min":73.0,"max":73.0}},
      {"address":"aws_db_instance.main","type":"aws_db_instance","name":"main","change":"noop","monthly":{"min":219.0,"max":219.0}}
    ],
    "unpriced":[
      {"address":"random_pet.name","type":"random_pet","change":"noop"}
    ],
    "state-version":{"id":"sv-xyz","serial":7,"created-at":"2026-07-20T09:00:00Z"}
  },
  "relationships":{"workspace":{"data":{"id":"ws-ws1","type":"workspaces"}}}}}`

const workspaceCostEmptyBody = `{"data":{"id":"workspace-cost-ws1","type":"workspace-cost-estimates",
  "attributes":{
    "currency":"USD",
    "total":{"min":0.0,"max":0.0},
    "previous":{"min":0.0,"max":0.0},
    "diff":{"min":0.0,"max":0.0},
    "resources":[],
    "unpriced":[],
    "state-version":null
  },
  "relationships":{"workspace":{"data":{"id":"ws-ws1","type":"workspaces"}}}}}`

func TestGetWorkspaceCostEstimate(t *testing.T) {
	c := newCostEstimateFixture(t, http.StatusOK, workspaceCostBody)
	e, err := c.GetWorkspaceCostEstimate(t.Context(), "ws1") // bare UUID → "ws-ws1"
	if err != nil {
		t.Fatal(err)
	}
	if e.WorkspaceID != "ws1" {
		t.Errorf("WorkspaceID = %q, want ws1 (prefix stripped)", e.WorkspaceID)
	}
	if e.Total.Max != 292.0 || e.Diff.Max != 0.0 {
		t.Errorf("ranges not decoded: total=%+v diff=%+v", e.Total, e.Diff)
	}
	if len(e.Resources) != 2 || len(e.Unpriced) != 1 {
		t.Fatalf("resources=%d unpriced=%d, want 2/1", len(e.Resources), len(e.Unpriced))
	}
	if e.Resources[0].Change != "noop" {
		t.Errorf("state resources must be noop, got %q", e.Resources[0].Change)
	}
	if e.StateVersion == nil || e.StateVersion.ID != "sv-xyz" || e.StateVersion.Serial != 7 {
		t.Errorf("state-version not decoded: %+v", e.StateVersion)
	}
}

func TestGetWorkspaceCostEstimateNoState(t *testing.T) {
	c := newCostEstimateFixture(t, http.StatusOK, workspaceCostEmptyBody)
	e, err := c.GetWorkspaceCostEstimate(t.Context(), "ws-ws1")
	if err != nil {
		t.Fatal(err)
	}
	if e.StateVersion != nil {
		t.Errorf("want nil StateVersion for a workspace with no state, got %+v", e.StateVersion)
	}
	if e.Total.Max != 0.0 || len(e.Resources) != 0 {
		t.Errorf("want zeroed empty estimate, got total=%+v resources=%d", e.Total, len(e.Resources))
	}
}

func TestGetWorkspaceCostEstimateEmptyID(t *testing.T) {
	c := newCostEstimateFixture(t, http.StatusOK, workspaceCostBody)
	if _, err := c.GetWorkspaceCostEstimate(t.Context(), ""); err == nil {
		t.Fatal("want error for empty workspace id")
	}
}

const costSummaryBody = `{"data":{"id":"cost-summary-abc","type":"cost-summaries",
  "attributes":{
    "status":"ready",
    "estimated-resources":[
      {"address":"azurerm_linux_virtual_machine.web","type":"azurerm_linux_virtual_machine","monthly":{"min":70.0,"max":90.0},"basis":"Standard_D2s_v5, West Europe, 730h","source":"ai-estimate"}
    ],
    "narrative":"Roughly $219/mo, dominated by aws_instance.eu.",
    "advisories":[
      {"kind":"reserved","title":"RI aws_instance.eu","detail":"On-demand 24/7; a 1yr RI cuts it.","monthly_saving":{"min":40.0,"max":60.0},"source":"ai-estimate"},
      {"kind":"rightsizing","title":"Downsize us","detail":"Idle most of the day.","monthly_saving":null,"source":"ai-estimate"}
    ],
    "model":"bedrock/claude","input-tokens":120,"output-tokens":60,"error-message":"",
    "language":"en","translated":true,
    "created-at":"2026-01-01T00:00:00Z","updated-at":"2026-01-01T00:01:00Z"
  },
  "relationships":{"run":{"data":{"id":"run-abc","type":"runs"}}}}}`

func TestGetRunCostSummary(t *testing.T) {
	c := newCostEstimateFixture(t, http.StatusOK, costSummaryBody)
	s, err := c.GetRunCostSummary(t.Context(), "abc")
	if err != nil {
		t.Fatal(err)
	}
	if s.RunID != "abc" {
		t.Errorf("RunID = %q, want abc", s.RunID)
	}
	if s.Status != "ready" || s.Narrative == "" {
		t.Errorf("status/narrative not decoded: %+v", s)
	}
	if s.Language != "en" || !s.Translated {
		t.Errorf("language/translated not decoded: lang=%q translated=%v", s.Language, s.Translated)
	}
	if len(s.EstimatedResources) != 1 {
		t.Fatalf("estimated_resources=%d, want 1", len(s.EstimatedResources))
	}
	er := s.EstimatedResources[0]
	if er.Address != "azurerm_linux_virtual_machine.web" || er.Source != "ai-estimate" {
		t.Errorf("estimated resource not decoded / provenance lost: %+v", er)
	}
	if er.Monthly.Min != 70.0 || er.Basis == "" {
		t.Errorf("estimated resource monthly/basis not decoded: %+v", er)
	}
	if len(s.Advisories) != 2 {
		t.Fatalf("advisories=%d, want 2", len(s.Advisories))
	}
	a := s.Advisories[0]
	if a.Kind != "reserved" || a.Source != "ai-estimate" {
		t.Errorf("advisory not decoded / provenance lost: %+v", a)
	}
	if a.MonthlySaving == nil || a.MonthlySaving.Min != 40.0 {
		t.Errorf("monthly_saving not decoded: %+v", a.MonthlySaving)
	}
	if s.Advisories[1].MonthlySaving != nil {
		t.Errorf("null monthly_saving should decode to nil, got %+v", s.Advisories[1].MonthlySaving)
	}
}

func TestGetRunCostSummaryNotFound(t *testing.T) {
	c := newCostEstimateFixture(t, http.StatusNotFound, "")
	if _, err := c.GetRunCostSummary(t.Context(), "run-none"); !IsNotFound(err) {
		t.Fatalf("want NotFoundError, got %v", err)
	}
}

func TestGetRunCostSummaryEmptyID(t *testing.T) {
	c := newCostEstimateFixture(t, http.StatusOK, costSummaryBody)
	if _, err := c.GetRunCostSummary(t.Context(), ""); err == nil {
		t.Fatal("want error for empty run id")
	}
}

func TestRegenerateRunCostSummary(t *testing.T) {
	// The regenerate endpoint returns 202 with the pending row.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("method = %s, want POST", r.Method)
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusAccepted)
		_, _ = w.Write([]byte(`{"data":{"id":"cost-summary-abc","type":"cost-summaries",
		  "attributes":{"status":"pending","narrative":"","advisories":[]},
		  "relationships":{"run":{"data":{"id":"run-abc","type":"runs"}}}}}`))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	s, err := c.RegenerateRunCostSummary(t.Context(), "abc")
	if err != nil {
		t.Fatal(err)
	}
	if s.Status != "pending" {
		t.Errorf("status = %q, want pending", s.Status)
	}
}

func TestRegenerateRunCostSummaryEmptyID(t *testing.T) {
	c := newCostEstimateFixture(t, http.StatusOK, costSummaryBody)
	if _, err := c.RegenerateRunCostSummary(t.Context(), ""); err == nil {
		t.Fatal("want error for empty run id")
	}
}

const costMessagesBody = `{"data":[
  {"id":"cost-summary-message-1","type":"cost-summary-messages","attributes":{"role":"user","content":"why is the broker so expensive?","input-tokens":0,"output-tokens":0}},
  {"id":"cost-summary-message-2","type":"cost-summary-messages","attributes":{"role":"assistant","content":"An mq.m5.large runs 24/7 at ~$0.30/hr.","model":"bedrock/claude","input-tokens":100,"output-tokens":25}}
],"meta":{"count":2,"language":"en"}}`

const costReplyBody = `{"data":{"id":"cost-summary-message-2","type":"cost-summary-messages",
  "attributes":{"role":"assistant","content":"An mq.m5.large runs 24/7 at ~$0.30/hr.","model":"bedrock/claude","input-tokens":100,"output-tokens":25}}}`

func newCostChatFixture(t *testing.T) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch r.Method {
		case http.MethodGet:
			_, _ = w.Write([]byte(costMessagesBody))
		case http.MethodPost:
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(costReplyBody))
		}
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestListRunCostSummaryMessages(t *testing.T) {
	c := newCostChatFixture(t)
	msgs, err := c.ListRunCostSummaryMessages(t.Context(), "abc")
	if err != nil {
		t.Fatal(err)
	}
	if len(msgs) != 2 {
		t.Fatalf("messages: %+v", msgs)
	}
	if msgs[0].Role != "user" || msgs[1].Role != "assistant" {
		t.Errorf("roles: %+v", msgs)
	}
	if msgs[1].OutputTokens != 25 {
		t.Errorf("output tokens: %+v", msgs[1])
	}
}

func TestPostRunCostSummaryMessage(t *testing.T) {
	c := newCostChatFixture(t)
	reply, err := c.PostRunCostSummaryMessage(t.Context(), "abc", "why is the broker so expensive?")
	if err != nil {
		t.Fatal(err)
	}
	if reply.Role != "assistant" || reply.OutputTokens != 25 {
		t.Errorf("reply: %+v", reply)
	}
}

func TestPostRunCostSummaryMessageGuards(t *testing.T) {
	c := newCostChatFixture(t)
	if _, err := c.PostRunCostSummaryMessage(t.Context(), "abc", ""); err == nil {
		t.Fatal("want error for empty content")
	}
	if _, err := c.PostRunCostSummaryMessage(t.Context(), "", "hi"); err == nil {
		t.Fatal("want error for empty run id")
	}
	if _, err := c.ListRunCostSummaryMessages(t.Context(), ""); err == nil {
		t.Fatal("want error for empty run id on list")
	}
}
