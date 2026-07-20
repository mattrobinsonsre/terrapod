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
