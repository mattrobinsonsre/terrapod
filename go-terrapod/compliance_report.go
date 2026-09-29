package terrapod

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"strconv"
)

// PolicyCheckSummary describes a summary of one policy check in a compliance report.
type PolicyCheckSummary struct {
	ID             string `json:"id"`
	Kind           string `json:"kind"`
	Status         string `json:"status"`
	Passed         int    `json:"passed"`
	SoftFailed     int    `json:"soft_failed"`
	AdvisoryFailed int    `json:"advisory_failed"`
}

// PolicyEvaluationDetail describes an OPA policy set evaluation detail.
type PolicyEvaluationDetail struct {
	PolicySetID      string  `json:"policy_set_id"`
	EnforcementLevel string  `json:"enforcement_level"`
	Outcome          string  `json:"outcome"`
	OverriddenBy     *string `json:"overridden_by,omitempty"`
	OverriddenAt     *string `json:"overridden_at,omitempty"`
}

// SecurityScanSummary describes security scanner results in a compliance report.
type SecurityScanSummary struct {
	Scanner       string `json:"scanner"`
	Enforced      bool   `json:"enforced"`
	Outcome       string `json:"outcome"`
	CriticalCount int    `json:"critical_count"`
	HighCount     int    `json:"high_count"`
	MediumCount   int    `json:"medium_count"`
	LowCount      int    `json:"low_count"`
}

// RunComplianceReport is an audit-ready compliance report for a single run.
type RunComplianceReport struct {
	ID                  string                   `json:"id"`
	RunID               string                   `json:"run_id"`
	WorkspaceID         string                   `json:"workspace_id"`
	Verdict             string                   `json:"verdict"`
	RunStatus           string                   `json:"run_status"`
	CreatedAt           string                   `json:"created_at"`
	ExecutionBackend    string                   `json:"execution_backend"`
	IsDestroy           bool                     `json:"is_destroy"`
	PlanOnly            bool                     `json:"plan_only"`
	PolicyChecksSummary []PolicyCheckSummary     `json:"policy_checks_summary"`
	PolicyEvaluations   []PolicyEvaluationDetail `json:"policy_evaluations"`
	SecurityScan        *SecurityScanSummary     `json:"security_scan,omitempty"`
}

// WorkspaceComplianceSummary carries aggregated counts across workspace runs.
type WorkspaceComplianceSummary struct {
	Compliant             int     `json:"compliant"`
	NonCompliant          int     `json:"non_compliant"`
	Overridden            int     `json:"overridden"`
	PendingReview         int     `json:"pending_review"`
	ComplianceRatePercent float64 `json:"compliance_rate_percent"`
}

// WorkspaceComplianceReport aggregates compliance reports across workspace runs.
type WorkspaceComplianceReport struct {
	WorkspaceID        string                     `json:"workspace_id"`
	TotalRunsEvaluated int                        `json:"total_runs_evaluated"`
	Summary            WorkspaceComplianceSummary `json:"summary"`
	Runs               []RunComplianceReport      `json:"runs"`
}

// GetRunComplianceReport fetches an audit-ready compliance report for a single run.
func (c *Client) GetRunComplianceReport(ctx context.Context, runID string) (*RunComplianceReport, error) {
	if runID == "" {
		return nil, errors.New("run id is required")
	}
	id := runID
	if len(id) > 4 && id[:4] != "run-" {
		id = "run-" + id
	}
	data, err := c.Get(ctx, "/api/v1/runs/"+url.PathEscape(id)+"/compliance-report")
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse compliance report response: %w", err)
	}
	report := &RunComplianceReport{}
	if raw, ok := res.Attributes["id"]; ok {
		_ = json.Unmarshal(raw, &report.ID)
	}
	if raw, ok := res.Attributes["run_id"]; ok {
		_ = json.Unmarshal(raw, &report.RunID)
	}
	if raw, ok := res.Attributes["workspace_id"]; ok {
		_ = json.Unmarshal(raw, &report.WorkspaceID)
	}
	if raw, ok := res.Attributes["verdict"]; ok {
		_ = json.Unmarshal(raw, &report.Verdict)
	}
	if raw, ok := res.Attributes["run_status"]; ok {
		_ = json.Unmarshal(raw, &report.RunStatus)
	}
	if raw, ok := res.Attributes["created_at"]; ok {
		_ = json.Unmarshal(raw, &report.CreatedAt)
	}
	if raw, ok := res.Attributes["execution_backend"]; ok {
		_ = json.Unmarshal(raw, &report.ExecutionBackend)
	}
	if raw, ok := res.Attributes["policy_checks_summary"]; ok {
		_ = json.Unmarshal(raw, &report.PolicyChecksSummary)
	}
	if raw, ok := res.Attributes["policy_evaluations"]; ok {
		_ = json.Unmarshal(raw, &report.PolicyEvaluations)
	}
	if raw, ok := res.Attributes["security_scan"]; ok {
		_ = json.Unmarshal(raw, &report.SecurityScan)
	}
	return report, nil
}

// GetWorkspaceComplianceReport fetches aggregate compliance reports for a workspace.
func (c *Client) GetWorkspaceComplianceReport(ctx context.Context, workspaceID string, limit int) (*WorkspaceComplianceReport, error) {
	if workspaceID == "" {
		return nil, errors.New("workspace id is required")
	}
	id := workspaceID
	if len(id) > 3 && id[:3] != "ws-" {
		id = "ws-" + id
	}
	endpoint := "/api/v1/workspaces/" + url.PathEscape(id) + "/compliance-report"
	if limit > 0 {
		endpoint += "?limit=" + strconv.Itoa(limit)
	}
	data, err := c.Get(ctx, endpoint)
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse workspace compliance report response: %w", err)
	}
	report := &WorkspaceComplianceReport{}
	if raw, ok := res.Attributes["workspace_id"]; ok {
		_ = json.Unmarshal(raw, &report.WorkspaceID)
	}
	if raw, ok := res.Attributes["total_runs_evaluated"]; ok {
		_ = json.Unmarshal(raw, &report.TotalRunsEvaluated)
	}
	if raw, ok := res.Attributes["summary"]; ok {
		_ = json.Unmarshal(raw, &report.Summary)
	}
	if raw, ok := res.Attributes["runs"]; ok {
		_ = json.Unmarshal(raw, &report.Runs)
	}
	return report, nil
}
