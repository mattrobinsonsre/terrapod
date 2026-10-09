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
	SoftFailed     int    `json:"soft-failed"`
	AdvisoryFailed int    `json:"advisory-failed"`
}

// PolicyEvaluationDetail describes an OPA policy set evaluation detail.
type PolicyEvaluationDetail struct {
	PolicySetID      *string `json:"policy-set-id,omitempty"`
	PolicySetName    string  `json:"policy-set-name"`
	EnforcementLevel string  `json:"enforcement-level"`
	Outcome          string  `json:"outcome"`
	OverriddenBy     *string `json:"overridden-by,omitempty"`
	OverriddenAt     *string `json:"overridden-at,omitempty"`
}

// SecurityScanSummary describes security scanner results in a compliance report.
type SecurityScanSummary struct {
	Engine            string  `json:"engine"`
	EnforcementLevel  string  `json:"enforcement-level"`
	SeverityThreshold string  `json:"severity-threshold"`
	Outcome           string  `json:"outcome"`
	CriticalCount     int     `json:"critical-count"`
	HighCount         int     `json:"high-count"`
	MediumCount       int     `json:"medium-count"`
	LowCount          int     `json:"low-count"`
	UnknownCount      int     `json:"unknown-count"`
	TotalCount        int     `json:"total-count"`
	BlockingCount     int     `json:"blocking-count"`
	Error             *string `json:"error,omitempty"`
	OverriddenBy      *string `json:"overridden-by,omitempty"`
	OverriddenAt      *string `json:"overridden-at,omitempty"`
}

// RunComplianceReport is an audit-ready compliance report for a single run.
type RunComplianceReport struct {
	ID                  string                   `json:"id"`
	RunID               string                   `json:"run-id"`
	WorkspaceID         string                   `json:"workspace-id"`
	Verdict             string                   `json:"verdict"`
	RunStatus           string                   `json:"run-status"`
	CreatedAt           string                   `json:"created-at"`
	ExecutionBackend    string                   `json:"execution-backend"`
	IsDestroy           bool                     `json:"is-destroy"`
	PlanOnly            bool                     `json:"plan-only"`
	PolicyChecksSummary []PolicyCheckSummary     `json:"policy-checks-summary"`
	PolicyEvaluations   []PolicyEvaluationDetail `json:"policy-evaluations"`
	SecurityScan        *SecurityScanSummary     `json:"security-scan,omitempty"`
}

// WorkspaceComplianceSummary carries aggregated counts across workspace runs.
type WorkspaceComplianceSummary struct {
	Compliant             int     `json:"compliant"`
	NonCompliant          int     `json:"non-compliant"`
	Overridden            int     `json:"overridden"`
	PendingReview         int     `json:"pending-review"`
	ComplianceRatePercent float64 `json:"compliance-rate-percent"`
}

// WorkspaceComplianceReport aggregates compliance reports across workspace runs.
type WorkspaceComplianceReport struct {
	WorkspaceID        string                     `json:"workspace-id"`
	TotalRunsEvaluated int                        `json:"total-runs-evaluated"`
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
	report := &RunComplianceReport{ID: res.ID}
	if raw, ok := res.Attributes["run-id"]; ok {
		_ = json.Unmarshal(raw, &report.RunID)
	}
	if raw, ok := res.Attributes["workspace-id"]; ok {
		_ = json.Unmarshal(raw, &report.WorkspaceID)
	}
	if raw, ok := res.Attributes["verdict"]; ok {
		_ = json.Unmarshal(raw, &report.Verdict)
	}
	if raw, ok := res.Attributes["run-status"]; ok {
		_ = json.Unmarshal(raw, &report.RunStatus)
	}
	if raw, ok := res.Attributes["created-at"]; ok {
		_ = json.Unmarshal(raw, &report.CreatedAt)
	}
	if raw, ok := res.Attributes["execution-backend"]; ok {
		_ = json.Unmarshal(raw, &report.ExecutionBackend)
	}
	if raw, ok := res.Attributes["is-destroy"]; ok {
		_ = json.Unmarshal(raw, &report.IsDestroy)
	}
	if raw, ok := res.Attributes["plan-only"]; ok {
		_ = json.Unmarshal(raw, &report.PlanOnly)
	}
	if raw, ok := res.Attributes["policy-checks-summary"]; ok {
		_ = json.Unmarshal(raw, &report.PolicyChecksSummary)
	}
	if raw, ok := res.Attributes["policy-evaluations"]; ok {
		_ = json.Unmarshal(raw, &report.PolicyEvaluations)
	}
	if raw, ok := res.Attributes["security-scan"]; ok {
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
	if raw, ok := res.Attributes["workspace-id"]; ok {
		_ = json.Unmarshal(raw, &report.WorkspaceID)
	}
	if raw, ok := res.Attributes["total-runs-evaluated"]; ok {
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
