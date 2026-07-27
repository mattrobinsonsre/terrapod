package terrapod

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

const architectureCritiqueBody = `{"data":{"id":"architecture-critique-abc","type":"architecture-critiques",
  "attributes":{
    "status":"ready",
    "state-serial":7,
    "risk-level":"high",
    "architecture":{
      "summary":"A single-AZ 2-tier web app: aws_instance.web fronting aws_db_instance.main.",
      "tiers":["web: aws_instance.web","data: aws_db_instance.main"],
      "data_stores":["aws_db_instance.main: primary Postgres"],
      "blast_radius":"aws_db_instance.main is single-AZ with no replica — a zone loss takes the app down."
    },
    "findings":[
      {"severity":"high","category":"reliability","title":"RDS is single-AZ",
       "detail":"aws_db_instance.main has multi_az=false; a zone failure loses the primary.",
       "resource_address":"aws_db_instance.main","recommendation":"Set multi_az=true.","grounded_in":"state"},
      {"severity":"medium","category":"security","title":"SG open to 0.0.0.0/0",
       "detail":"aws_security_group.web allows 0.0.0.0/0 on 22.",
       "resource_address":"aws_security_group.web","recommendation":"Restrict to a bastion CIDR.","grounded_in":"CKV_AWS_24"}
    ],
    "deferred":["Backup retention on aws_db_instance.main — not present in the provided attributes."],
    "model":"bedrock/anthropic.claude-opus-4-8",
    "input-tokens":1200,
    "output-tokens":340,
    "created-at":"2026-01-01T00:00:00Z",
    "updated-at":"2026-01-01T00:00:05Z"
  }}}`

func newArchitectureCritiqueFixture(t *testing.T, status int, body string) (*Client, *string) {
	t.Helper()
	var lastMethod string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		lastMethod = r.Method
		w.Header().Set("Content-Type", "application/vnd.api+json")
		if status != http.StatusOK && status != http.StatusAccepted {
			w.WriteHeader(status)
			_, _ = w.Write([]byte(`{"errors":[{"detail":"no critique"}]}`))
			return
		}
		if r.Method == http.MethodPost {
			w.WriteHeader(http.StatusAccepted)
			_, _ = w.Write([]byte(`{"data":{"id":"architecture-critique-regenerate-ws-abc",` +
				`"type":"architecture-critiques","attributes":{"status":"pending"}}}`))
			return
		}
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c, &lastMethod
}

func TestGetArchitectureCritique(t *testing.T) {
	c, _ := newArchitectureCritiqueFixture(t, http.StatusOK, architectureCritiqueBody)
	cr, err := c.GetArchitectureCritique(t.Context(), "abc") // bare UUID → "ws-abc"
	if err != nil {
		t.Fatal(err)
	}
	if cr.Status != "ready" {
		t.Errorf("Status = %q, want ready", cr.Status)
	}
	if cr.StateSerial != 7 {
		t.Errorf("StateSerial = %d, want 7", cr.StateSerial)
	}
	if cr.RiskLevel != "high" {
		t.Errorf("RiskLevel = %q, want high", cr.RiskLevel)
	}
	if cr.Architecture.Summary == "" || len(cr.Architecture.Tiers) != 2 ||
		len(cr.Architecture.DataStores) != 1 || cr.Architecture.BlastRadius == "" {
		t.Errorf("architecture not decoded: %+v", cr.Architecture)
	}
	if len(cr.Findings) != 2 {
		t.Fatalf("Findings = %d, want 2", len(cr.Findings))
	}
	f0 := cr.Findings[0]
	if f0.Severity != "high" || f0.Category != "reliability" ||
		f0.ResourceAddress != "aws_db_instance.main" || f0.GroundedIn != "state" {
		t.Errorf("finding[0] not decoded: %+v", f0)
	}
	if cr.Findings[1].GroundedIn != "CKV_AWS_24" {
		t.Errorf("finding[1] grounded_in = %q, want CKV_AWS_24 (scanner rule id)", cr.Findings[1].GroundedIn)
	}
	if len(cr.Deferred) != 1 {
		t.Errorf("Deferred = %d, want 1", len(cr.Deferred))
	}
	if cr.InputTokens != 1200 || cr.OutputTokens != 340 {
		t.Errorf("tokens not decoded: in=%d out=%d", cr.InputTokens, cr.OutputTokens)
	}
}

func TestGetArchitectureCritiqueNotFound(t *testing.T) {
	c, _ := newArchitectureCritiqueFixture(t, http.StatusNotFound, "")
	_, err := c.GetArchitectureCritique(t.Context(), "ws-none")
	if err == nil {
		t.Fatal("expected error for 404")
	}
	if !IsNotFound(err) {
		t.Errorf("expected NotFoundError, got %T: %v", err, err)
	}
}

func TestGetArchitectureCritiqueEmptyID(t *testing.T) {
	c, _ := newArchitectureCritiqueFixture(t, http.StatusOK, architectureCritiqueBody)
	if _, err := c.GetArchitectureCritique(t.Context(), ""); err == nil {
		t.Fatal("expected error for empty workspace id")
	}
}

func TestRegenerateArchitectureCritique(t *testing.T) {
	c, method := newArchitectureCritiqueFixture(t, http.StatusAccepted, "")
	cr, err := c.RegenerateArchitectureCritique(t.Context(), "abc")
	if err != nil {
		t.Fatal(err)
	}
	if *method != http.MethodPost {
		t.Errorf("method = %q, want POST", *method)
	}
	if cr.Status != "pending" {
		t.Errorf("Status = %q, want pending", cr.Status)
	}
}
