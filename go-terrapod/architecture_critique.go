package terrapod

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"strings"
)

// CritiqueFinding is one discrete issue the AI architecture critic raised about
// a run's proposed infrastructure (#963/#1036). Severity is one of
// critical|high|medium|low|info; Category is one of
// security|reliability|cost|operations|scalability|other; Address is the
// terraform resource address the finding attaches to (optional — omitted for a
// cross-cutting observation). Every field here is AI judgement, not a computed
// fact; render it as advisory review, not authoritative truth.
type CritiqueFinding struct {
	Severity string `json:"severity"`
	Category string `json:"category"`
	Title    string `json:"title"`
	Detail   string `json:"detail"`
	Address  string `json:"address,omitempty"`
}

// ArchitectureCritique is the optional AI *architecture review* of a run's
// proposed infrastructure (#963/#1036). A senior cloud/platform architect
// persona reviews the run's planned values and returns a prose Critique plus a
// set of structured Findings and an overall RiskLevel. It is the architecture
// analogue of CostSummary: everything here is AI judgement/polish, generated
// asynchronously from the run's stored plan JSON (never state), and surfaced on
// the run page's Security tab.
type ArchitectureCritique struct {
	// ID is the prefixed resource id ("architecture-critique-<uuid>"); RunID is
	// the bare run UUID resolved from the `run` relationship.
	ID        string `json:"-"`
	RunID     string `json:"-"`
	Status    string `json:"status"` // pending|ready|errored|skipped
	// Critique is the prose narrative review of the proposed infrastructure.
	Critique string `json:"critique"`
	// RiskLevel is the model's overall risk rating: critical|high|medium|low|none.
	RiskLevel string `json:"risk-level"`
	// Findings are the discrete, structured issues the critic raised.
	Findings     []CritiqueFinding `json:"findings"`
	Model        string            `json:"model"`
	InputTokens  int               `json:"input-tokens"`
	OutputTokens int               `json:"output-tokens"`
	ErrorMessage string            `json:"error-message"`
	// Language is the canonical language the prose is stored in; Translated is
	// true when the served critique/finding text was translated on view for a
	// reader locale (mirrors CostSummary).
	Language   string `json:"-"`
	Translated bool   `json:"-"`
	CreatedAt  string `json:"created-at"`
	UpdatedAt  string `json:"updated-at"`
}

func architectureCritiqueFromResource(res *Resource) *ArchitectureCritique {
	c := &ArchitectureCritique{
		ID:           res.ID,
		RunID:        strings.TrimPrefix(GetRelationshipID(res, "run"), "run-"),
		Status:       GetStringAttr(res, "status"),
		Critique:     GetStringAttr(res, "critique"),
		RiskLevel:    GetStringAttr(res, "risk-level"),
		Model:        GetStringAttr(res, "model"),
		InputTokens:  int(GetIntAttr(res, "input-tokens")),
		OutputTokens: int(GetIntAttr(res, "output-tokens")),
		ErrorMessage: GetStringAttr(res, "error-message"),
		Language:     GetStringAttr(res, "language"),
		Translated:   GetBoolAttr(res, "translated"),
		CreatedAt:    GetStringAttr(res, "created-at"),
		UpdatedAt:    GetStringAttr(res, "updated-at"),
	}
	if raw, ok := res.Attributes["findings"]; ok {
		_ = json.Unmarshal(raw, &c.Findings)
	}
	return c
}

// GetRunArchitectureCritique fetches the AI architecture critique for a run
// (#963/#1036).
//
// runID accepts either a bare run UUID or the prefixed "run-<uuid>" form.
//
// Returns *NotFoundError when no critique exists yet (AI disabled, workspace
// opted out, or not generated). A `pending` status means it is in flight.
func (c *Client) GetRunArchitectureCritique(ctx context.Context, runID string) (*ArchitectureCritique, error) {
	if runID == "" {
		return nil, errors.New("run id is required")
	}
	id := runID
	if len(id) > 4 && id[:4] != "run-" {
		id = "run-" + id
	}
	data, err := c.Get(ctx, "/api/terrapod/v1/runs/"+url.PathEscape(id)+"/architecture-critique")
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse architecture critique response: %w", err)
	}
	return architectureCritiqueFromResource(res), nil
}

// RegenerateRunArchitectureCritique re-fires the AI architecture critique for a
// run (#963/#1036). Returns the upserted pending row immediately; the model
// call runs asynchronously (listen for the `architecture_critique_ready` SSE
// event, or poll GetRunArchitectureCritique).
//
// Returns *ConflictError when the run has no plan to review, and the equivalent
// of a 503 when AI summary is globally disabled.
func (c *Client) RegenerateRunArchitectureCritique(ctx context.Context, runID string) (*ArchitectureCritique, error) {
	if runID == "" {
		return nil, errors.New("run id is required")
	}
	id := runID
	if len(id) > 4 && id[:4] != "run-" {
		id = "run-" + id
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/runs/"+url.PathEscape(id)+"/architecture-critique/regenerate", nil)
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse regenerate architecture critique response: %w", err)
	}
	return architectureCritiqueFromResource(res), nil
}

// ArchitectureCritiqueMessage is one turn in a run's AI architecture-critique
// chat thread (#963/#1036). The initial critique/findings live on the parent
// ArchitectureCritique; these are the conversational follow-ups grounded in
// that review.
type ArchitectureCritiqueMessage struct {
	ID           string `json:"id"`
	Role         string `json:"role"` // "user" or "assistant"
	Content      string `json:"content"`
	Model        string `json:"model,omitempty"`
	InputTokens  int    `json:"input-tokens"`
	OutputTokens int    `json:"output-tokens"`
	ErrorMessage string `json:"error-message,omitempty"`
	CreatedAt    string `json:"created-at,omitempty"`
}

func architectureCritiqueMessageFromResource(res *Resource) *ArchitectureCritiqueMessage {
	return &ArchitectureCritiqueMessage{
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

// ListRunArchitectureCritiqueMessages returns the full architecture-critique
// chat transcript for a run in chronological order (empty when no follow-ups
// posted). Returns *NotFoundError when no critique exists, *ConflictError when
// it isn't ready.
func (c *Client) ListRunArchitectureCritiqueMessages(ctx context.Context, runID string) ([]*ArchitectureCritiqueMessage, error) {
	if runID == "" {
		return nil, errors.New("run id is required")
	}
	id := runID
	if len(id) > 4 && id[:4] != "run-" {
		id = "run-" + id
	}
	data, err := c.Get(ctx, "/api/terrapod/v1/runs/"+url.PathEscape(id)+"/architecture-critique/messages")
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, fmt.Errorf("parse architecture critique messages response: %w", err)
	}
	out := make([]*ArchitectureCritiqueMessage, 0, len(resources))
	for i := range resources {
		r := resources[i]
		out = append(out, architectureCritiqueMessageFromResource(&r))
	}
	return out, nil
}

// PostRunArchitectureCritiqueMessage posts an operator follow-up question about
// a run's architecture critique and returns the synchronous assistant reply.
// Read-on-workspace auth. Error mappings mirror the plan-summary chat (409
// cap/unready, 429 budget, 503 disabled, 400 empty/oversize, 502 model
// failure).
func (c *Client) PostRunArchitectureCritiqueMessage(ctx context.Context, runID, content string) (*ArchitectureCritiqueMessage, error) {
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
	body, err := MarshalResource("architecture-critique-messages", map[string]any{"content": content}, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal architecture critique message: %w", err)
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/runs/"+url.PathEscape(id)+"/architecture-critique/messages", body)
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse post-architecture-critique-message response: %w", err)
	}
	return architectureCritiqueMessageFromResource(res), nil
}
