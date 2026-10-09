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
  "total-runs-in-workspace":7,
  "summary":{"compliant":1,"non-compliant":0,"overridden":0,"pending-review":0,"compliance-rate-percent":100},
  "runs":[]
},"relationships":{"workspace":{"data":{"id":"ws-1111","type":"workspaces"}}}}}`

// An unevaluated workspace. The rate is null, not a number -- see the pointer's
// comment in compliance_report.go.
const emptyWorkspaceComplianceResponseBody = `{"data":{"id":"ws-cmpl-ws-2222","type":"workspace-compliance-reports","attributes":{
  "workspace-id":"ws-2222",
  "total-runs-evaluated":0,
  "total-runs-in-workspace":0,
  "summary":{"compliant":0,"non-compliant":0,"overridden":0,"pending-review":0,"compliance-rate-percent":null},
  "runs":[]
},"relationships":{"workspace":{"data":{"id":"ws-2222","type":"workspaces"}}}}}`

func TestGetRunComplianceReport(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p := r.URL.Path
		switch {
		case r.Method == http.MethodGet && p == "/api/v1/runs/run-1111/compliance-report":
			_, _ = w.Write([]byte(runComplianceResponseBody))
		case r.Method == http.MethodGet && p == "/api/v1/workspaces/ws-1111/compliance-report":
			_, _ = w.Write([]byte(workspaceComplianceResponseBody))
		case r.Method == http.MethodGet && p == "/api/v1/workspaces/ws-2222/compliance-report":
			_, _ = w.Write([]byte(emptyWorkspaceComplianceResponseBody))
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
	// The sample size is only meaningful against what it was drawn from.
	if wsReport.TotalRunsInWorkspace != 7 {
		t.Errorf("expected total-runs-in-workspace 7, got %d", wsReport.TotalRunsInWorkspace)
	}
	if wsReport.Summary.ComplianceRatePercent == nil {
		t.Fatal("expected a compliance rate, got nil")
	}
	if *wsReport.Summary.ComplianceRatePercent != 100 {
		t.Errorf("expected compliance rate 100, got %f", *wsReport.Summary.ComplianceRatePercent)
	}

	// An unevaluated workspace reports no rate at all. As a float64 this
	// field would read 0.0 here -- total non-compliance for a workspace
	// nobody has run, which is the one answer an audit report must not give.
	empty, err := c.GetWorkspaceComplianceReport(t.Context(), "ws-2222", 50)
	if err != nil {
		t.Fatalf("GetWorkspaceComplianceReport (empty) failed: %v", err)
	}
	if empty.Summary.ComplianceRatePercent != nil {
		t.Errorf("expected no compliance rate for an unevaluated workspace, got %f",
			*empty.Summary.ComplianceRatePercent)
	}
	if empty.TotalRunsInWorkspace != 0 {
		t.Errorf("expected total-runs-in-workspace 0, got %d", empty.TotalRunsInWorkspace)
	}
}
