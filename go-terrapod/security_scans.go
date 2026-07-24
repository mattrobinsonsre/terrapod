package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
)

// SecurityScan is the deterministic IaC security-scan result attached to a
// single run by the Checkov/Trivy scan phase (#1036) — the structural twin of
// an OPA PolicyEvaluation but with prebuilt scanner rules instead of Rego.
//
// Outcome is the engine truth ("passed" | "failed" | "errored"); whether a
// "failed"/"errored" scan actually blocks the run depends on the workspace's
// EnforcementLevel ("off" | "advisory" | "enforced"), snapshotted here at scan
// time. An "enforced" failure holds the run in planning until an admin calls
// OverrideRunSecurityScan or the finding is fixed.
type SecurityScan struct {
	ID                string                `json:"id"`
	RunID             string                `json:"-"` // resolved from the run path, not the body
	Engine            string                `json:"engine"`
	EnforcementLevel  string                `json:"enforcement-level"`
	SeverityThreshold string                `json:"severity-threshold"`
	Outcome           string                `json:"outcome"`
	Findings          []SecurityScanFinding `json:"findings,omitempty"`
	Summary           map[string]any        `json:"summary,omitempty"`
	Error             string                `json:"error,omitempty"`
	OverriddenBy      string                `json:"overridden-by,omitempty"`
	OverriddenAt      string                `json:"overridden-at,omitempty"`
	CreatedAt         string                `json:"created-at,omitempty"`
}

// SecurityScanFinding is one normalised misconfiguration finding. Both engines
// (Checkov CKV_*, Trivy AVD-*) map into this shape; Severity is normalised to
// critical|high|medium|low (unrated scanner findings default to "high").
type SecurityScanFinding struct {
	Engine    string `json:"engine"`
	RuleID    string `json:"rule_id"`
	Severity  string `json:"severity"`
	Title     string `json:"title"`
	Resource  string `json:"resource"`
	File      string `json:"file"`
	Line      int    `json:"line"`
	Guideline string `json:"guideline"`
}

// GetRunSecurityScan fetches the security-scan result recorded for a run.
//
// Returns (nil, nil) when no scan has been recorded for the run — the endpoint
// answers 200 with a null data body in that case (the workspace has scanning
// off, or the run hasn't been scanned yet), which is not an error.
func (c *Client) GetRunSecurityScan(ctx context.Context, runID string) (*SecurityScan, error) {
	id, err := runIDPath(runID)
	if err != nil {
		return nil, err
	}
	data, err := c.Get(ctx, "/api/terrapod/v1/runs/"+id+"/security-scan")
	if err != nil {
		return nil, err
	}
	return securityScanFromBody(data, runID)
}

// OverrideRunSecurityScan overrides a blocking security scan for a run (requires
// workspace admin). A run still held in planning by the scan gate is re-driven
// immediately. Returns the (now overridden) scan, or (nil, nil) if none was
// recorded.
func (c *Client) OverrideRunSecurityScan(ctx context.Context, runID string) (*SecurityScan, error) {
	id, err := runIDPath(runID)
	if err != nil {
		return nil, err
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/runs/"+id+"/actions/override-security-scan", nil)
	if err != nil {
		return nil, err
	}
	return securityScanFromBody(data, runID)
}

// securityScanFromBody decodes the {"data": <resource>|null, "meta": ...} body.
// A null data element means "no scan recorded" → (nil, nil).
func securityScanFromBody(data []byte, runID string) (*SecurityScan, error) {
	var envelope struct {
		Data json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(data, &envelope); err != nil {
		return nil, fmt.Errorf("parse security-scan response: %w", err)
	}
	if len(envelope.Data) == 0 || string(envelope.Data) == "null" {
		return nil, nil
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse security-scan resource: %w", err)
	}
	return securityScanFromResource(res, runID), nil
}

func securityScanFromResource(res *Resource, runID string) *SecurityScan {
	id := runID
	if len(id) > 4 && id[:4] == "run-" {
		id = id[4:]
	}
	s := &SecurityScan{
		ID:                res.ID,
		RunID:             id,
		Engine:            GetStringAttr(res, "engine"),
		EnforcementLevel:  GetStringAttr(res, "enforcement-level"),
		SeverityThreshold: GetStringAttr(res, "severity-threshold"),
		Outcome:           GetStringAttr(res, "outcome"),
		Error:             GetStringAttr(res, "error"),
		OverriddenBy:      GetStringAttr(res, "overridden-by"),
		OverriddenAt:      GetStringAttr(res, "overridden-at"),
		CreatedAt:         GetStringAttr(res, "created-at"),
	}
	if raw, ok := res.Attributes["findings"]; ok {
		_ = json.Unmarshal(raw, &s.Findings)
	}
	if raw, ok := res.Attributes["summary"]; ok {
		_ = json.Unmarshal(raw, &s.Summary)
	}
	return s
}
