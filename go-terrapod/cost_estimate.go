package terrapod

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"strings"
)

// CostRange is a monthly cost interval (min == max when a resource has a single
// deterministic price; they differ when usage bounds produce a range).
type CostRange struct {
	Min float64 `json:"min"`
	Max float64 `json:"max"`
}

// CostResource is one priced resource in a plan's cost estimate.
type CostResource struct {
	Address string    `json:"address"`
	Type    string    `json:"type"`
	Name    string    `json:"name"`
	Change  string    `json:"change"` // noop | add | remove
	Monthly CostRange `json:"monthly"`
	// UsageAssumptions are the usage-driven line items (requests, data
	// processed, duration) whose quantity was assumed, not read from the plan —
	// each with a low/typical/high band. Priced at the typical; the band is the
	// honest raw range (usable with the AI enhancement off), which the AI then
	// narrows per-resource. Empty/omitted for a fully deterministic resource.
	UsageAssumptions []UsageAssumption `json:"usage_assumptions,omitempty"`
}

// UsageAssumption describes one usage-driven cost component whose quantity was a
// low/typical/high judgement rather than a fact from the plan (#962).
//
// Low/Typical/High are the assumed *quantity* (e.g. GB/month); CostLow/
// CostTypical/CostHigh are the resulting monthly *cost* in the estimate's
// currency at each of those quantities — the honest dollar range of the
// assumption (usable with the AI off). The Cost* fields are pointers: nil when
// the server couldn't price the band (rare — a misconfigured or unpriceable
// component), distinct from a genuine $0.
type UsageAssumption struct {
	Description string  `json:"description"`
	Dimension   string  `json:"dimension"`
	Unit        string  `json:"unit"`
	Low         float64 `json:"low"`
	Typical     float64 `json:"typical"`
	High        float64 `json:"high"`

	CostLow     *float64 `json:"cost_low,omitempty"`
	CostTypical *float64 `json:"cost_typical,omitempty"`
	CostHigh    *float64 `json:"cost_high,omitempty"`
}

// UnpricedResource is a resource nothing in the pricesheet matched (unmapped
// type, a non-priced/free resource, or a provider not covered by the data).
type UnpricedResource struct {
	Address string `json:"address"`
	Type    string `json:"type"`
	Change  string `json:"change"`
}

// CostEstimate is the native cost estimate of a plan's monthly
// cost, derived server-side from the run's stored plan JSON and served to the
// run-page Cost tab (#871).
//
// Every figure here is DATA (engine-derived) — no AI is involved. Total is the
// projected monthly spend of the planned state, Diff is the monthly delta this
// run introduces (adds positive, removes negative), and Previous is Total-Diff.
type CostEstimate struct {
	// RunID is the bare run UUID (the "run-" prefix is stripped), resolved
	// from the `run` relationship.
	RunID     string             `json:"-"`
	Currency  string             `json:"currency"`
	Total     CostRange          `json:"total"`
	Previous  CostRange          `json:"previous"`
	Diff      CostRange          `json:"diff"`
	Resources []CostResource     `json:"resources"`
	Unpriced  []UnpricedResource `json:"unpriced"`
}

// GetRunCostEstimate fetches the cost estimate for a run.
//
// runID accepts either a bare run UUID or the prefixed "run-<uuid>" form.
//
// Returns *NotFoundError when the run produced no cost estimate (errored before
// plan, cost estimation disabled for the run, or the artifact aged out).
func (c *Client) GetRunCostEstimate(ctx context.Context, runID string) (*CostEstimate, error) {
	if runID == "" {
		return nil, errors.New("run id is required")
	}
	id := runID
	if len(id) > 4 && id[:4] != "run-" {
		id = "run-" + id
	}
	data, err := c.Get(ctx, "/api/terrapod/v1/runs/"+url.PathEscape(id)+"/cost-estimate")
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse cost estimate response: %w", err)
	}
	e := &CostEstimate{RunID: strings.TrimPrefix(GetRelationshipID(res, "run"), "run-")}
	for key, dst := range map[string]any{
		"currency":  &e.Currency,
		"total":     &e.Total,
		"previous":  &e.Previous,
		"diff":      &e.Diff,
		"resources": &e.Resources,
		"unpriced":  &e.Unpriced,
	} {
		if raw, ok := res.Attributes[key]; ok {
			_ = json.Unmarshal(raw, dst)
		}
	}
	return e, nil
}

// CostStateVersion identifies the state version a workspace cost estimate was
// derived from. Nil on a WorkspaceCostEstimate when the workspace has no state.
type CostStateVersion struct {
	ID        string `json:"id"` // prefixed "sv-<uuid>"
	Serial    int    `json:"serial"`
	CreatedAt string `json:"created-at"`
}

// WorkspaceCostEstimate is the native cost estimate of a
// workspace's CURRENT managed infrastructure, derived server-side from its
// latest Terraform state (#871). It is the state analogue of CostEstimate's
// plan-delta view: Total is the current projected monthly spend, Diff is zero
// (state carries no change), and every resource is a "noop".
//
// Every figure is DATA (engine-derived) — no AI involved.
type WorkspaceCostEstimate struct {
	// WorkspaceID is the bare workspace UUID (the "ws-" prefix is stripped),
	// resolved from the `workspace` relationship.
	WorkspaceID string             `json:"-"`
	Currency    string             `json:"currency"`
	Total       CostRange          `json:"total"`
	Previous    CostRange          `json:"previous"`
	Diff        CostRange          `json:"diff"`
	Resources   []CostResource     `json:"resources"`
	Unpriced    []UnpricedResource `json:"unpriced"`
	// StateVersion names the priced state version; nil when the workspace has
	// no state yet (Total is zero in that case).
	StateVersion *CostStateVersion `json:"state-version"`
}

// GetWorkspaceCostEstimate fetches the current managed-infrastructure cost
// estimate for a workspace, computed from its latest state version.
//
// workspaceID accepts either a bare workspace UUID or the prefixed "ws-<uuid>"
// form. A workspace with no state returns a zeroed estimate with a nil
// StateVersion (not an error). Returns *NotFoundError when cost estimation is
// disabled globally.
func (c *Client) GetWorkspaceCostEstimate(ctx context.Context, workspaceID string) (*WorkspaceCostEstimate, error) {
	if workspaceID == "" {
		return nil, errors.New("workspace id is required")
	}
	id := workspaceID
	if len(id) < 3 || id[:3] != "ws-" {
		id = "ws-" + id
	}
	data, err := c.Get(ctx, "/api/terrapod/v1/workspaces/"+url.PathEscape(id)+"/cost-estimate")
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse workspace cost estimate response: %w", err)
	}
	e := &WorkspaceCostEstimate{WorkspaceID: strings.TrimPrefix(GetRelationshipID(res, "workspace"), "ws-")}
	for key, dst := range map[string]any{
		"currency":      &e.Currency,
		"total":         &e.Total,
		"previous":      &e.Previous,
		"diff":          &e.Diff,
		"resources":     &e.Resources,
		"unpriced":      &e.Unpriced,
		"state-version": &e.StateVersion,
	} {
		if raw, ok := res.Attributes[key]; ok {
			_ = json.Unmarshal(raw, dst)
		}
	}
	return e, nil
}

// CostAdvisory is one AI-suggested savings opportunity attached to a run's cost
// narrative (#871). It is AI *polish*, never an authoritative figure:
// MonthlySaving is the model's ESTIMATE and Source is always "ai-estimate"
// (stamped server-side); render it distinctly from the deterministic cost figures and
// never treat it as a computed total.
type CostAdvisory struct {
	Kind          string     `json:"kind"` // savings_plan|reserved|spot|rightsizing|other
	Title         string     `json:"title"`
	Detail        string     `json:"detail"`
	MonthlySaving *CostRange `json:"monthly_saving"` // nil when the model gave no figure
	Source        string     `json:"source"`         // always "ai-estimate"
}

// CostEstimatedResource is an AI monthly estimate for a resource the
// deterministic engine could NOT price (#871) — the PRIMARY output of the
// cost AI. Monthly is always an ESTIMATE (Source == "ai-estimate"), shown as a
// separate overlay and never summed into the authoritative deterministic total; Basis is
// the model's one-line justification/assumption.
type CostEstimatedResource struct {
	Address string    `json:"address"`
	Type    string    `json:"type"`
	Monthly CostRange `json:"monthly"`
	Basis   string    `json:"basis"`
	Source  string    `json:"source"` // always "ai-estimate"
}

// CostSummary is the optional AI *enhancement* of a run's cost estimate (#871).
// Its PRIMARY value is EstimatedResources — the model pricing what the engine couldn't
// (the unpriced set + providers the engine doesn't cover + usage-driven costs). The
// narrative + advisories are the secondary, human-readable bonus. Everything
// here is AI estimate/polish — the authoritative figures live on CostEstimate
// (GetRunCostEstimate) and are never restated.
type CostSummary struct {
	// ID is the prefixed resource id ("cost-summary-<uuid>"); RunID is the bare
	// run UUID resolved from the `run` relationship.
	ID                 string                  `json:"-"`
	RunID              string                  `json:"-"`
	Status             string                  `json:"status"` // pending|ready|errored|skipped
	EstimatedResources []CostEstimatedResource `json:"estimated-resources"`
	Narrative          string                  `json:"narrative"`
	Advisories         []CostAdvisory          `json:"advisories"`
	Model              string                  `json:"model"`
	InputTokens        int                     `json:"input-tokens"`
	OutputTokens       int                     `json:"output-tokens"`
	ErrorMessage       string                  `json:"error-message"`
	// Language is the canonical language the prose is stored in; Translated is
	// true when the served narrative/basis/advisory text was translated on view
	// for a reader locale (#871). Request a locale via GetRunCostSummaryLocale.
	Language   string `json:"-"`
	Translated bool   `json:"-"`
	CreatedAt  string `json:"created-at"`
	UpdatedAt  string `json:"updated-at"`
}

func costSummaryFromResource(res *Resource) *CostSummary {
	s := &CostSummary{
		ID:           res.ID,
		RunID:        strings.TrimPrefix(GetRelationshipID(res, "run"), "run-"),
		Status:       GetStringAttr(res, "status"),
		Narrative:    GetStringAttr(res, "narrative"),
		Model:        GetStringAttr(res, "model"),
		InputTokens:  int(GetIntAttr(res, "input-tokens")),
		OutputTokens: int(GetIntAttr(res, "output-tokens")),
		ErrorMessage: GetStringAttr(res, "error-message"),
		Language:     GetStringAttr(res, "language"),
		Translated:   GetBoolAttr(res, "translated"),
		CreatedAt:    GetStringAttr(res, "created-at"),
		UpdatedAt:    GetStringAttr(res, "updated-at"),
	}
	if raw, ok := res.Attributes["estimated-resources"]; ok {
		_ = json.Unmarshal(raw, &s.EstimatedResources)
	}
	if raw, ok := res.Attributes["advisories"]; ok {
		_ = json.Unmarshal(raw, &s.Advisories)
	}
	return s
}

// GetRunCostSummary fetches the AI cost narrative for a run (#871).
//
// runID accepts either a bare run UUID or the prefixed "run-<uuid>" form.
//
// Returns *NotFoundError when no narrative exists yet (AI disabled, workspace
// opted out, or not generated). A `pending` status means it is in flight.
func (c *Client) GetRunCostSummary(ctx context.Context, runID string) (*CostSummary, error) {
	if runID == "" {
		return nil, errors.New("run id is required")
	}
	id := runID
	if len(id) > 4 && id[:4] != "run-" {
		id = "run-" + id
	}
	data, err := c.Get(ctx, "/api/terrapod/v1/runs/"+url.PathEscape(id)+"/cost-summary")
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse cost summary response: %w", err)
	}
	return costSummaryFromResource(res), nil
}

// RegenerateRunCostSummary re-fires the AI cost narrative for a run (#871).
// Returns the upserted pending row immediately; the model call runs
// asynchronously (listen for the `cost_summary_ready` SSE event, or poll
// GetRunCostSummary).
//
// Returns *ConflictError when the run has no cost estimate to narrate, and the
// equivalent of a 503 when AI summary is globally disabled.
func (c *Client) RegenerateRunCostSummary(ctx context.Context, runID string) (*CostSummary, error) {
	if runID == "" {
		return nil, errors.New("run id is required")
	}
	id := runID
	if len(id) > 4 && id[:4] != "run-" {
		id = "run-" + id
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/runs/"+url.PathEscape(id)+"/cost-summary/regenerate", nil)
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse regenerate cost summary response: %w", err)
	}
	return costSummaryFromResource(res), nil
}

// CostSummaryMessage is one turn in a run's AI cost-estimate chat thread (#871).
// The initial estimate/advisories live on the parent CostSummary; these are the
// conversational follow-ups grounded in that estimate.
type CostSummaryMessage struct {
	ID           string `json:"id"`
	Role         string `json:"role"` // "user" or "assistant"
	Content      string `json:"content"`
	Model        string `json:"model,omitempty"`
	InputTokens  int    `json:"input-tokens"`
	OutputTokens int    `json:"output-tokens"`
	ErrorMessage string `json:"error-message,omitempty"`
	CreatedAt    string `json:"created-at,omitempty"`
}

// ListRunCostSummaryMessages returns the full cost-chat transcript for a run in
// chronological order (empty when no follow-ups posted). Returns *NotFoundError
// when no cost estimate exists, *ConflictError when it isn't ready.
func (c *Client) ListRunCostSummaryMessages(ctx context.Context, runID string) ([]*CostSummaryMessage, error) {
	if runID == "" {
		return nil, errors.New("run id is required")
	}
	id := runID
	if len(id) > 4 && id[:4] != "run-" {
		id = "run-" + id
	}
	data, err := c.Get(ctx, "/api/terrapod/v1/runs/"+url.PathEscape(id)+"/cost-summary/messages")
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, fmt.Errorf("parse cost messages response: %w", err)
	}
	out := make([]*CostSummaryMessage, 0, len(resources))
	for i := range resources {
		r := resources[i]
		out = append(out, costSummaryMessageFromResource(&r))
	}
	return out, nil
}

// PostRunCostSummaryMessage posts an operator follow-up question about a run's
// cost estimate and returns the synchronous assistant reply. Read-on-workspace
// auth. Error mappings mirror the plan-summary chat (409 cap/unready, 429 budget,
// 503 disabled, 400 empty/oversize, 502 model failure).
func (c *Client) PostRunCostSummaryMessage(ctx context.Context, runID, content string) (*CostSummaryMessage, error) {
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
	body, err := MarshalResource("cost-summary-messages", map[string]any{"content": content}, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal cost message: %w", err)
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/runs/"+url.PathEscape(id)+"/cost-summary/messages", body)
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse post-cost-message response: %w", err)
	}
	return costSummaryMessageFromResource(res), nil
}

func costSummaryMessageFromResource(res *Resource) *CostSummaryMessage {
	return &CostSummaryMessage{
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
