package terrapod

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func newArchitectureCritiqueFixture(t *testing.T, status int, body string) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		if status != http.StatusOK {
			w.WriteHeader(status)
			_, _ = w.Write([]byte(`{"errors":[{"detail":"no architecture critique for this run"}]}`))
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

const architectureCritiqueBody = `{"data":{"id":"architecture-critique-abc","type":"architecture-critiques",
  "attributes":{
    "status":"ready",
    "critique":"The proposed VPC exposes the RDS instance to 0.0.0.0/0; tighten the security group.",
    "risk-level":"high",
    "findings":[
      {"severity":"critical","category":"security","title":"Public database","detail":"aws_db_instance.main is reachable from the internet.","address":"aws_db_instance.main"},
      {"severity":"medium","category":"reliability","title":"Single AZ","detail":"No multi-AZ failover configured."}
    ],
    "model":"bedrock/claude","input-tokens":320,"output-tokens":140,"error-message":"",
    "language":"en","translated":true,
    "created-at":"2026-01-01T00:00:00Z","updated-at":"2026-01-01T00:01:00Z"
  },
  "relationships":{"run":{"data":{"id":"run-abc","type":"runs"}}}}}`

func TestGetRunArchitectureCritique(t *testing.T) {
	c := newArchitectureCritiqueFixture(t, http.StatusOK, architectureCritiqueBody)
	crit, err := c.GetRunArchitectureCritique(t.Context(), "abc") // bare UUID → "run-abc"
	if err != nil {
		t.Fatal(err)
	}
	if crit.RunID != "abc" {
		t.Errorf("RunID = %q, want abc (prefix stripped)", crit.RunID)
	}
	if crit.Status != "ready" || crit.Critique == "" {
		t.Errorf("status/critique not decoded: %+v", crit)
	}
	if crit.RiskLevel != "high" {
		t.Errorf("RiskLevel = %q, want high", crit.RiskLevel)
	}
	if crit.Language != "en" || !crit.Translated {
		t.Errorf("language/translated not decoded: lang=%q translated=%v", crit.Language, crit.Translated)
	}
	if crit.Model != "bedrock/claude" || crit.InputTokens != 320 || crit.OutputTokens != 140 {
		t.Errorf("model/tokens not decoded: %+v", crit)
	}
	if len(crit.Findings) != 2 {
		t.Fatalf("findings=%d, want 2", len(crit.Findings))
	}
	f := crit.Findings[0]
	if f.Severity != "critical" || f.Category != "security" || f.Title != "Public database" ||
		f.Address != "aws_db_instance.main" {
		t.Errorf("finding[0] not decoded: %+v", f)
	}
	if crit.Findings[1].Address != "" {
		t.Errorf("optional address should be empty when omitted, got %q", crit.Findings[1].Address)
	}
}

func TestGetRunArchitectureCritiqueNotFound(t *testing.T) {
	c := newArchitectureCritiqueFixture(t, http.StatusNotFound, "")
	if _, err := c.GetRunArchitectureCritique(t.Context(), "run-none"); !IsNotFound(err) {
		t.Fatalf("want NotFoundError, got %v", err)
	}
}

func TestGetRunArchitectureCritiqueEmptyID(t *testing.T) {
	c := newArchitectureCritiqueFixture(t, http.StatusOK, architectureCritiqueBody)
	if _, err := c.GetRunArchitectureCritique(t.Context(), ""); err == nil {
		t.Fatal("want error for empty run id")
	}
}

func TestRegenerateRunArchitectureCritique(t *testing.T) {
	// The regenerate endpoint returns 202 with the pending row.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("method = %s, want POST", r.Method)
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusAccepted)
		_, _ = w.Write([]byte(`{"data":{"id":"architecture-critique-abc","type":"architecture-critiques",
		  "attributes":{"status":"pending","critique":"","risk-level":"none","findings":[]},
		  "relationships":{"run":{"data":{"id":"run-abc","type":"runs"}}}}}`))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	crit, err := c.RegenerateRunArchitectureCritique(t.Context(), "abc")
	if err != nil {
		t.Fatal(err)
	}
	if crit.Status != "pending" {
		t.Errorf("status = %q, want pending", crit.Status)
	}
	if len(crit.Findings) != 0 {
		t.Errorf("pending row should carry no findings, got %+v", crit.Findings)
	}
}

func TestRegenerateRunArchitectureCritiqueConflict(t *testing.T) {
	c := newArchitectureCritiqueFixture(t, http.StatusConflict, "")
	if _, err := c.RegenerateRunArchitectureCritique(t.Context(), "abc"); !IsConflict(err) {
		t.Fatalf("want ConflictError, got %v", err)
	}
}

func TestRegenerateRunArchitectureCritiqueEmptyID(t *testing.T) {
	c := newArchitectureCritiqueFixture(t, http.StatusOK, architectureCritiqueBody)
	if _, err := c.RegenerateRunArchitectureCritique(t.Context(), ""); err == nil {
		t.Fatal("want error for empty run id")
	}
}

const architectureCritiqueMessagesBody = `{"data":[
  {"id":"architecture-critique-message-1","type":"architecture-critique-messages","attributes":{"role":"user","content":"how do I fix the public database?","input-tokens":0,"output-tokens":0}},
  {"id":"architecture-critique-message-2","type":"architecture-critique-messages","attributes":{"role":"assistant","content":"Scope the security group ingress to your VPC CIDR.","model":"bedrock/claude","input-tokens":140,"output-tokens":45}}
],"meta":{"count":2,"language":"en"}}`

const architectureCritiqueReplyBody = `{"data":{"id":"architecture-critique-message-2","type":"architecture-critique-messages",
  "attributes":{"role":"assistant","content":"Scope the security group ingress to your VPC CIDR.","model":"bedrock/claude","input-tokens":140,"output-tokens":45}}}`

func newArchitectureCritiqueChatFixture(t *testing.T) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch r.Method {
		case http.MethodGet:
			_, _ = w.Write([]byte(architectureCritiqueMessagesBody))
		case http.MethodPost:
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(architectureCritiqueReplyBody))
		}
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestListRunArchitectureCritiqueMessages(t *testing.T) {
	c := newArchitectureCritiqueChatFixture(t)
	msgs, err := c.ListRunArchitectureCritiqueMessages(t.Context(), "abc")
	if err != nil {
		t.Fatal(err)
	}
	if len(msgs) != 2 {
		t.Fatalf("messages: %+v", msgs)
	}
	if msgs[0].Role != "user" || msgs[1].Role != "assistant" {
		t.Errorf("roles: %+v", msgs)
	}
	if msgs[1].OutputTokens != 45 {
		t.Errorf("output tokens: %+v", msgs[1])
	}
}

func TestListRunArchitectureCritiqueMessagesNotFound(t *testing.T) {
	c := newArchitectureCritiqueFixture(t, http.StatusNotFound, "")
	if _, err := c.ListRunArchitectureCritiqueMessages(t.Context(), "abc"); !IsNotFound(err) {
		t.Fatalf("want NotFoundError, got %v", err)
	}
}

func TestPostRunArchitectureCritiqueMessage(t *testing.T) {
	c := newArchitectureCritiqueChatFixture(t)
	reply, err := c.PostRunArchitectureCritiqueMessage(t.Context(), "abc", "how do I fix the public database?")
	if err != nil {
		t.Fatal(err)
	}
	if reply.Role != "assistant" || reply.OutputTokens != 45 {
		t.Errorf("reply: %+v", reply)
	}
}

func TestPostRunArchitectureCritiqueMessageError(t *testing.T) {
	c := newArchitectureCritiqueFixture(t, http.StatusConflict, "")
	if _, err := c.PostRunArchitectureCritiqueMessage(t.Context(), "abc", "hi"); !IsConflict(err) {
		t.Fatalf("want ConflictError, got %v", err)
	}
}

func TestPostRunArchitectureCritiqueMessageGuards(t *testing.T) {
	c := newArchitectureCritiqueChatFixture(t)
	if _, err := c.PostRunArchitectureCritiqueMessage(t.Context(), "abc", ""); err == nil {
		t.Fatal("want error for empty content")
	}
	if _, err := c.PostRunArchitectureCritiqueMessage(t.Context(), "", "hi"); err == nil {
		t.Fatal("want error for empty run id")
	}
	if _, err := c.ListRunArchitectureCritiqueMessages(t.Context(), ""); err == nil {
		t.Fatal("want error for empty run id on list")
	}
}
