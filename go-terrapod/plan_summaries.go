package terrapod

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
)

// PlanSummary is the AI-generated description + risk assessment (or
// failure analysis) attached to a single plan, produced by the
// optional ai_summary feature (#401).
//
// Kind distinguishes the two skills:
//   - "plan_summary": successful plan → describe proposed changes
//     and rate their risk
//   - "failure_analysis": errored plan → explain why the plan
//     failed and suggest fixes
//
// Field reuse across kinds:
//   - Description: change summary OR failure explanation
//   - RiskLevel:   change-risk severity OR failure severity
//   - RiskFactors: discrete risks OR suggested fixes (same shape;
//     the UI / your consumer decides how to render them)
type PlanSummary struct {
	ID          string `json:"id"`
	RunID       string `json:"-"` // resolved from `run` relationship
	Kind        string `json:"kind"`
	Status      string `json:"status"`
	Description string `json:"description,omitempty"`
	RiskLevel   string `json:"risk-level,omitempty"`
	RiskFactors []PlanSummaryRiskFactor `json:"risk-factors,omitempty"`

	// Telemetry / debugging
	Model        string `json:"model,omitempty"`
	InputTokens  int    `json:"input-tokens"`
	OutputTokens int    `json:"output-tokens"`
	ErrorMessage string `json:"error-message,omitempty"`

	CreatedAt string `json:"created-at,omitempty"`
	UpdatedAt string `json:"updated-at,omitempty"`
}

// PlanSummaryRiskFactor is one entry in PlanSummary.RiskFactors.
// For Kind="plan_summary" entries describe a risk; for
// Kind="failure_analysis" entries describe a suggested fix.
type PlanSummaryRiskFactor struct {
	Severity        string `json:"severity"`
	Title           string `json:"title"`
	Detail          string `json:"detail"`
	ResourceAddress string `json:"resource_address,omitempty"`
}

// GetPlanSummary fetches the AI summary for one plan.
//
// planID accepts either a bare run UUID or the prefixed "plan-<uuid>"
// form — both resolve to the same plan.
//
// Returns *NotFoundError when no summary exists yet (the feature is
// off for this workspace, the plan hasn't been summarised, or the
// summariser was skipped). Returns the row as-is for any other status
// — callers should branch on s.Status:
//
//	switch s.Status {
//	case "ready":    // s.Description, s.RiskLevel, s.RiskFactors are populated
//	case "pending":  // in flight — retry later or wait on the SSE event
//	case "skipped":  // workspace opted out or daily budget hit
//	case "errored":  // s.ErrorMessage holds the failure reason
//	}
func (c *Client) GetPlanSummary(ctx context.Context, planID string) (*PlanSummary, error) {
	if planID == "" {
		return nil, errors.New("plan id is required")
	}
	id := planID
	if len(id) > 5 && id[:5] != "plan-" {
		id = "plan-" + id
	}
	data, err := c.Get(ctx, "/api/v2/plans/"+url.PathEscape(id)+"/summary")
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse plan summary response: %w", err)
	}
	return planSummaryFromResource(res), nil
}

func planSummaryFromResource(res *Resource) *PlanSummary {
	s := &PlanSummary{
		ID:           res.ID,
		Kind:         GetStringAttr(res, "kind"),
		Status:       GetStringAttr(res, "status"),
		Description:  GetStringAttr(res, "description"),
		RiskLevel:    GetStringAttr(res, "risk-level"),
		Model:        GetStringAttr(res, "model"),
		InputTokens:  int(GetIntAttr(res, "input-tokens")),
		OutputTokens: int(GetIntAttr(res, "output-tokens")),
		ErrorMessage: GetStringAttr(res, "error-message"),
		CreatedAt:    GetStringAttr(res, "created-at"),
		UpdatedAt:    GetStringAttr(res, "updated-at"),
	}
	// Run relationship → expose as bare UUID for ergonomic round-tripping
	// with the rest of the SDK (Run.ID is "run-<uuid>"; we strip the prefix
	// here so callers can pass either form to GetRun).
	s.RunID = GetRelationshipID(res, "run")

	if raw, ok := res.Attributes["risk-factors"]; ok {
		// risk-factors arrives as a JSON array of objects; unmarshal
		// directly into the slice rather than hand-walking interface
		// values. The server clamps each field, so we don't bother
		// re-validating here.
		_ = json.Unmarshal(raw, &s.RiskFactors)
	}
	return s
}

// RegeneratePlanSummary re-fires the AI summary handler for a run.
// Returns the upserted pending row immediately; the actual model call
// runs asynchronously. Listen for the `plan_summary_ready` SSE event
// to know when the new summary lands, or poll GetPlanSummary.
//
// Returns *ConflictError when the run is in a state with no
// summarisable output yet (still planning, plan-phase errored before
// the log was produced). Returns the equivalent of a 503 from
// /-Service when AI summary is globally disabled in the deployment.
func (c *Client) RegeneratePlanSummary(ctx context.Context, runID string) (*PlanSummary, error) {
	if runID == "" {
		return nil, errors.New("run id is required")
	}
	id := runID
	if len(id) > 4 && id[:4] != "run-" {
		id = "run-" + id
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/runs/"+url.PathEscape(id)+"/plan-summary/regenerate", nil)
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse regenerate response: %w", err)
	}
	return planSummaryFromResource(res), nil
}

// PlanSummaryMessage is one turn in the AI plan-summary chat thread
// (#463). The initial structured summary lives on the parent
// PlanSummary row (Description + RiskFactors); these messages are the
// conversational follow-ups that build on top of it.
type PlanSummaryMessage struct {
	ID           string `json:"id"`
	Role         string `json:"role"`    // "user" or "assistant"
	Content      string `json:"content"`
	Model        string `json:"model,omitempty"`
	InputTokens  int    `json:"input-tokens"`
	OutputTokens int    `json:"output-tokens"`
	ErrorMessage string `json:"error-message,omitempty"`
	CreatedAt    string `json:"created-at,omitempty"`
}

// ListPlanSummaryMessages returns the full chat transcript for a run
// in chronological order. The list excludes the initial structured
// summary (which lives on the parent PlanSummary); messages here are
// only the conversational follow-ups.
//
// Returns an empty slice when no follow-ups have been posted yet.
// Returns *NotFoundError if no initial summary exists.
// Returns *ConflictError if the initial summary is still pending or
// errored — can't chat against an unready summary.
func (c *Client) ListPlanSummaryMessages(ctx context.Context, runID string) ([]*PlanSummaryMessage, error) {
	if runID == "" {
		return nil, errors.New("run id is required")
	}
	id := runID
	if len(id) > 4 && id[:4] != "run-" {
		id = "run-" + id
	}
	data, err := c.Get(ctx, "/api/terrapod/v1/runs/"+url.PathEscape(id)+"/plan-summary/messages")
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, fmt.Errorf("parse messages response: %w", err)
	}
	out := make([]*PlanSummaryMessage, 0, len(resources))
	for i := range resources {
		r := resources[i]
		out = append(out, planSummaryMessageFromResource(&r))
	}
	return out, nil
}

// PostPlanSummaryMessage posts a user follow-up question and returns
// the synchronous assistant reply. Authorisation is read-on-workspace
// — anyone with read on the workspace can chat in the thread
// (matches PR conversation semantics, not per-user threads).
//
// Common error mappings:
//   - 409 → *ConflictError (initial summary not ready, or per-run cap hit)
//   - 429 → *APIError ("daily AI token budget exhausted")
//   - 503 → *APIError (chat globally disabled or workspace opted out)
//   - 400 → *ValidationError (empty body / oversize body)
//   - 502 → *APIError (model HTTP / parse failure — the user turn is
//     still persisted in the transcript, and a separate errored
//     assistant row is recorded so reload shows the failure cleanly)
func (c *Client) PostPlanSummaryMessage(ctx context.Context, runID, content string) (*PlanSummaryMessage, error) {
	if runID == "" {
		return nil, errors.New("run id is required")
	}
	if content == "" {
		return nil, errors.New("content is required")
	}
	id := runID
	if len(id) > 4 && id[:4] != "run-" {
		id = "run-" + id
	}
	body, err := MarshalResource("plan-summary-messages", map[string]any{"content": content}, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal message: %w", err)
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/runs/"+url.PathEscape(id)+"/plan-summary/messages", body)
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse post-message response: %w", err)
	}
	return planSummaryMessageFromResource(res), nil
}

func planSummaryMessageFromResource(res *Resource) *PlanSummaryMessage {
	return &PlanSummaryMessage{
		ID:           res.ID,
		Role:         GetStringAttr(res, "role"),
		Content:      GetStringAttr(res, "content"),
		Model:        GetStringAttr(res, "model"),
		InputTokens:  int(GetIntAttr(res, "input-tokens")),
		OutputTokens: int(GetIntAttr(res, "output-tokens")),
		ErrorMessage: GetStringAttr(res, "error-message"),
		CreatedAt:    GetStringAttr(res, "created-at"),
	}
}
