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
}

// UnpricedResource is a resource nothing in the pricesheet matched (unmapped
// type, a non-priced/free resource, or a provider not covered by the data).
type UnpricedResource struct {
	Address string `json:"address"`
	Type    string `json:"type"`
	Change  string `json:"change"`
}

// CostEstimate is the native OpenInfraQuote-port estimate of a plan's monthly
// cost, derived server-side from the run's stored plan JSON and served to the
// run-page Cost tab (#871).
//
// Every figure here is DATA (oiq-derived) — no AI is involved. Total is the
// projected monthly spend of the planned state, Diff is the monthly delta this
// run introduces (adds positive, removes negative), and Previous is Total-Diff.
//
// Credit: the pricing data + matcher/pricer design are OpenInfraQuote's (by
// Terrateam); Terrapod ships a native reader engine and consumes their
// prices.csv.
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
	ID        string `json:"id"`     // prefixed "sv-<uuid>"
	Serial    int    `json:"serial"`
	CreatedAt string `json:"created-at"`
}

// WorkspaceCostEstimate is the native OpenInfraQuote-port estimate of a
// workspace's CURRENT managed infrastructure, derived server-side from its
// latest Terraform state (#871). It is the state analogue of CostEstimate's
// plan-delta view: Total is the current projected monthly spend, Diff is zero
// (state carries no change), and every resource is a "noop".
//
// Every figure is DATA (oiq-derived) — no AI involved. Credit: pricing data +
// matcher/pricer design are OpenInfraQuote's (by Terrateam).
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
// (stamped server-side); render it distinctly from the oiq cost figures and
// never treat it as a computed total.
type CostAdvisory struct {
	Kind          string     `json:"kind"` // savings_plan|reserved|spot|rightsizing|other
	Title         string     `json:"title"`
	Detail        string     `json:"detail"`
	MonthlySaving *CostRange `json:"monthly_saving"` // nil when the model gave no figure
	Source        string     `json:"source"`         // always "ai-estimate"
}

// CostSummary is the optional AI *enhancement* of a run's cost estimate (#871):
// a plain-language narrative plus savings advisories. It rides the plan-analysis
// AI switch and holds AI polish ONLY — the authoritative monthly figures live on
// CostEstimate (GetRunCostEstimate) and are never restated here.
type CostSummary struct {
	// ID is the prefixed resource id ("cost-summary-<uuid>"); RunID is the bare
	// run UUID resolved from the `run` relationship.
	ID           string         `json:"-"`
	RunID        string         `json:"-"`
	Status       string         `json:"status"` // pending|ready|errored|skipped
	Narrative    string         `json:"narrative"`
	Advisories   []CostAdvisory `json:"advisories"`
	Model        string         `json:"model"`
	InputTokens  int            `json:"input-tokens"`
	OutputTokens int            `json:"output-tokens"`
	ErrorMessage string         `json:"error-message"`
	CreatedAt    string         `json:"created-at"`
	UpdatedAt    string         `json:"updated-at"`
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
		CreatedAt:    GetStringAttr(res, "created-at"),
		UpdatedAt:    GetStringAttr(res, "updated-at"),
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
