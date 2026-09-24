package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
)

// AIPolicyEvaluation is the AI policy gate's verdict for a single run (#1766)
// — the judgement-call sibling of an OPA PolicyEvaluation and a SecurityScan.
//
// The verdict rides the plan summary's own model call rather than a separate
// one, so an evaluation always has a sibling plan summary and gating costs no
// additional tokens.
//
// Outcome is the ruling ("passed" | "failed" | "errored"); whether a
// "failed"/"errored" verdict actually blocks depends on EnforcementLevel
// ("advisory" | "mandatory"), snapshotted here at evaluation time. A mandatory
// failure holds the run in planning until an admin calls OverrideRunAIPolicy.
//
// "errored" is a BLOCKING outcome under a mandatory gate, not a soft failure:
// the gate fails closed, because a verdict that could not be reached is not
// consent. Error carries the reason in the operator's words — budget
// exhaustion reads differently from a model fault, deliberately, because the
// two need different responses.
type AIPolicyEvaluation struct {
	ID               string           `json:"id"`
	RunID            string           `json:"-"` // resolved from the run path, not the body
	EnforcementLevel string           `json:"enforcement-level"`
	RiskThreshold    string           `json:"risk-threshold"`
	Outcome          string           `json:"outcome"`
	Verdict          *AIPolicyVerdict `json:"verdict,omitempty"`
	RiskLevel        string           `json:"risk-level,omitempty"`
	Error            string           `json:"error,omitempty"`
	OverriddenBy     string           `json:"overridden-by,omitempty"`
	OverriddenAt     string           `json:"overridden-at,omitempty"`
	CreatedAt        string           `json:"created-at,omitempty"`
}

// AIPolicyVerdict is the model's ruling, stored verbatim so an operator
// reading a blocked run sees what the model was asked and what it answered.
type AIPolicyVerdict struct {
	Decision string                  `json:"decision"` // "allow" | "deny"
	Reasons  []AIPolicyVerdictReason `json:"reasons,omitempty"`
}

// AIPolicyVerdictReason names one matched criterion and what in the plan
// matched it. A deny always carries at least one: a block an operator cannot
// act on is worse than no gate.
type AIPolicyVerdictReason struct {
	Criterion string `json:"criterion"`
	Detail    string `json:"detail"`
}

// GetRunAIPolicy fetches the AI policy verdict recorded for a run.
//
// Returns (nil, nil) when nothing has been recorded — the endpoint answers 200
// with a null data body, which is not an error. That case is ambiguous on its
// own (the gate may be off, the engine may not be ruled on, or the verdict may
// simply not have landed yet), so the response's meta carries a
// not-evaluated-reason saying which.
func (c *Client) GetRunAIPolicy(ctx context.Context, runID string) (*AIPolicyEvaluation, error) {
	id, err := runIDPath(runID)
	if err != nil {
		return nil, err
	}
	data, err := c.Get(ctx, "/api/v1/runs/"+id+"/ai-policy")
	if err != nil {
		return nil, err
	}
	return aiPolicyFromBody(data, runID)
}

// OverrideRunAIPolicy releases a run held by the AI policy gate (requires
// workspace admin). A run still held in planning is re-driven immediately.
//
// A run held with NO verdict recorded is released too, and is the case that
// most needs it: the summariser never ran, or failed before ruling, so the
// verdict a mandatory gate is waiting for can never arrive. That writes an
// explicit no-verdict override -- honest about never having ruled -- rather
// than a forged pass, so an auditor can tell the two apart.
func (c *Client) OverrideRunAIPolicy(ctx context.Context, runID string) (*AIPolicyEvaluation, error) {
	id, err := runIDPath(runID)
	if err != nil {
		return nil, err
	}
	data, err := c.Post(ctx, "/api/v1/runs/"+id+"/actions/override-ai-policy", nil)
	if err != nil {
		return nil, err
	}
	return aiPolicyFromBody(data, runID)
}

// aiPolicyFromBody decodes the {"data": <resource>|null, "meta": ...} body.
// A null data element means "no verdict recorded" → (nil, nil).
func aiPolicyFromBody(data []byte, runID string) (*AIPolicyEvaluation, error) {
	var envelope struct {
		Data json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(data, &envelope); err != nil {
		return nil, fmt.Errorf("parse ai-policy response: %w", err)
	}
	if len(envelope.Data) == 0 || string(envelope.Data) == "null" {
		return nil, nil
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse ai-policy resource: %w", err)
	}
	return aiPolicyFromResource(res, runID), nil
}

func aiPolicyFromResource(res *Resource, runID string) *AIPolicyEvaluation {
	id := runID
	if len(id) > 4 && id[:4] == "run-" {
		id = id[4:]
	}
	e := &AIPolicyEvaluation{
		ID:               res.ID,
		RunID:            id,
		EnforcementLevel: GetStringAttr(res, "enforcement-level"),
		RiskThreshold:    GetStringAttr(res, "risk-threshold"),
		Outcome:          GetStringAttr(res, "outcome"),
		RiskLevel:        GetStringAttr(res, "risk-level"),
		Error:            GetStringAttr(res, "error"),
		OverriddenBy:     GetStringAttr(res, "overridden-by"),
		OverriddenAt:     GetStringAttr(res, "overridden-at"),
		CreatedAt:        GetStringAttr(res, "created-at"),
	}
	if raw, ok := res.Attributes["verdict"]; ok {
		var v AIPolicyVerdict
		if err := json.Unmarshal(raw, &v); err == nil && v.Decision != "" {
			e.Verdict = &v
		}
	}
	return e
}

// IsBlocking reports whether this evaluation is holding the run.
//
// An overridden evaluation never blocks, and an advisory one never blocks
// whatever its outcome — which is why this is a method rather than a caller
// comparing Outcome to "failed" and getting the other two conditions wrong.
func (e *AIPolicyEvaluation) IsBlocking() bool {
	if e == nil {
		return false
	}
	if e.EnforcementLevel != "mandatory" {
		return false
	}
	if e.OverriddenBy != "" {
		return false
	}
	return e.Outcome == "failed" || e.Outcome == "errored"
}
