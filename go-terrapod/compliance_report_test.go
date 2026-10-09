package terrapod

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

const runComplianceResponseBody = `{"data":{"id":"cmpl-run-1111","type":"compliance-reports","attributes":{
  "id":"cmpl-run-1111",
  "run-id":"run-1111",
  "workspace-id":"ws-1111",
  "verdict":"COMPLIANT",
  "run-status":"applied",
  "created-at":"2026-10-07T12:00:00Z",
  "execution-backend":"tofu",
  "is-destroy":false,
  "plan-only":false,
  "policy-checks-summary":[],
  "policy-evaluations":[]
},"relationships":{"run":{"data":{"id":"run-1111","type":"runs"}}}}}`

const workspaceComplianceResponseBody = `{"data":{"id":"ws-cmpl-ws-1111","type":"workspace-compliance-reports","attributes":{
  "workspace-id":"ws-1111",
  "total-runs-evaluated":1,
  "summary":{"compliant":1,"non-compliant":0,"overridden":0,"pending-review":0,"compliance-rate-percent":100},
  "runs":[]
},"relationships":{"workspace":{"data":{"id":"ws-1111","type":"workspaces"}}}}}`

func TestGetRunComplianceReport(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p := r.URL.Path
		switch {
		case r.Method == http.MethodGet && p == "/api/v1/runs/run-1111/compliance-report":
			_, _ = w.Write([]byte(runComplianceResponseBody))
		case r.Method == http.MethodGet && p == "/api/v1/workspaces/ws-1111/compliance-report":
			_, _ = w.Write([]byte(workspaceComplianceResponseBody))
		default:
			http.Error(w, `{"errors":[{"status":"404"}]}`, http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)

	c, err := NewClient(Options{BaseURL: srv.URL, Token: "test-token"})
	if err != nil {
		t.Fatal(err)
	}

	report, err := c.GetRunComplianceReport(t.Context(), "run-1111")
	if err != nil {
		t.Fatalf("GetRunComplianceReport failed: %v", err)
	}
	if report.Verdict != "COMPLIANT" {
		t.Errorf("expected verdict COMPLIANT, got %s", report.Verdict)
	}
	if report.RunID != "run-1111" {
		t.Errorf("expected run-id run-1111, got %s", report.RunID)
	}

	wsReport, err := c.GetWorkspaceComplianceReport(t.Context(), "ws-1111", 50)
	if err != nil {
		t.Fatalf("GetWorkspaceComplianceReport failed: %v", err)
	}
	if wsReport.TotalRunsEvaluated != 1 {
		t.Errorf("expected total-runs-evaluated 1, got %d", wsReport.TotalRunsEvaluated)
	}
	if wsReport.Summary.ComplianceRatePercent != 100 {
		t.Errorf("expected compliance rate 100, got %f", wsReport.Summary.ComplianceRatePercent)
	}
}
