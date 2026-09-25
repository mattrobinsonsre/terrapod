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

// AIPolicyStatus is the whole answer to "is this gate holding the run, and
// why" — the evaluation if one exists, plus the response meta that says what
// the evaluation alone cannot.
//
// It exists because the row is not enough. A mandatory gate holds a run in
// BOTH states: a verdict that denied, and no verdict at all (the summariser
// has not answered, or never will). Reading only the row answers "nothing is
// wrong" for the second, which is the state that most needs an answer — the
// run sits indefinitely and OverrideRunAIPolicy is the control that releases
// it. The server computes the authoritative answer and puts it in meta;
// Blocking carries it through rather than recomputing it here.
type AIPolicyStatus struct {
	// Evaluation is nil when no verdict has been recorded. That is not an
	// error, and it does not mean the gate is idle — see Blocking.
	Evaluation *AIPolicyEvaluation

	// Blocking is the server's own answer to "is this gate what holds the
	// run", true even when Evaluation is nil.
	Blocking bool

	// NotEvaluatedReason distinguishes "waiting for a verdict" from "no
	// verdict is coming" when Evaluation is nil. Empty when one exists.
	NotEvaluatedReason string

	// EnforcementLevel is the workspace's CURRENT setting. It can differ from
	// Evaluation.EnforcementLevel, which is snapshotted at evaluation time —
	// that divergence is the point of snapshotting, so the two are kept apart.
	EnforcementLevel string

	// RunStatus is the run's status when the gate was queried.
	RunStatus string
}

// IsBlocking reports whether the gate is holding the run. Unlike the method of
// the same name on AIPolicyEvaluation, this is correct when no verdict has
// been recorded, because it reads the server's answer rather than a row.
func (s *AIPolicyStatus) IsBlocking() bool {
	return s != nil && s.Blocking
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

// GetRunAIPolicyStatus fetches the verdict AND the server's answer to whether
// the gate is holding the run.
//
// Prefer this over GetRunAIPolicy whenever the question is "is anything
// blocking this run": GetRunAIPolicy returns (nil, nil) for a run held with no
// verdict recorded, and AIPolicyEvaluation.IsBlocking on that nil is false —
// so the one state that most needs surfacing reads as "nothing is wrong".
func (c *Client) GetRunAIPolicyStatus(ctx context.Context, runID string) (*AIPolicyStatus, error) {
	id, err := runIDPath(runID)
	if err != nil {
		return nil, err
	}
	data, err := c.Get(ctx, "/api/terrapod/v1/runs/"+id+"/ai-policy")
	if err != nil {
		return nil, err
	}
	return aiPolicyStatusFromBody(data, runID)
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
	st, err := aiPolicyStatusFromBody(data, runID)
	if err != nil {
		return nil, err
	}
	return st.Evaluation, nil
}

// aiPolicyStatusFromBody decodes BOTH halves of the body. The meta half is
// what makes a held-with-no-verdict run visible, so it is not optional
// decoding — it is the part the row cannot tell you.
func aiPolicyStatusFromBody(data []byte, runID string) (*AIPolicyStatus, error) {
	var envelope struct {
		Data json.RawMessage `json:"data"`
		Meta struct {
			Blocking           bool   `json:"blocking"`
			NotEvaluatedReason string `json:"not-evaluated-reason"`
			EnforcementLevel   string `json:"enforcement-level"`
			RunStatus          string `json:"run-status"`
		} `json:"meta"`
	}
	if err := json.Unmarshal(data, &envelope); err != nil {
		return nil, fmt.Errorf("parse ai-policy response: %w", err)
	}
	st := &AIPolicyStatus{
		Blocking:           envelope.Meta.Blocking,
		NotEvaluatedReason: envelope.Meta.NotEvaluatedReason,
		EnforcementLevel:   envelope.Meta.EnforcementLevel,
		RunStatus:          envelope.Meta.RunStatus,
	}
	if len(envelope.Data) == 0 || string(envelope.Data) == "null" {
		return st, nil
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse ai-policy resource: %w", err)
	}
	st.Evaluation = aiPolicyFromResource(res, runID)
	return st, nil
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
//
// It answers only for a verdict that EXISTS. A mandatory gate also holds a run
// with no verdict at all, and this reports false on the nil receiver you get
// in that case. Use Client.GetRunAIPolicyStatus and AIPolicyStatus.IsBlocking
// when the question is "is this run held", rather than "did this ruling deny".
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
