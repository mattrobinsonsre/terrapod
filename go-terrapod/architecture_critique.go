package terrapod

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
)

// ArchitectureCritique is the AI architecture critic's assessment of a
// workspace's CURRENT deployed system, inferred from its latest Terraform
// state and critiqued across resilience, security, cost, and well-architected
// dimensions (#1036 Part 2, the optional ai_architecture feature).
//
// It is distinct from the per-run plan summary (#401), which reviews a
// *change* (a plan). The critique reviews the *system as it exists* (from
// state) and is regenerated when a new state version lands.
//
// Every dimension is grounded in deterministic data, never invented: security
// findings come from the Part 1 scanner (Checkov/Trivy) and carry the rule id
// in GroundedIn; cost findings from the cost engine; resilience/operations from
// state + the resource graph (AI judgment). Findings the model could not judge
// from the data are surfaced in Deferred rather than guessed.
//
// Callers branch on Status:
//
//	switch cr.Status {
//	case "ready":   // Architecture, RiskLevel, Findings are populated
//	case "pending": // in flight — wait on the SSE event or poll
//	case "skipped": // workspace has no state, or the daily budget was hit
//	case "errored": // cr.ErrorMessage holds the failure reason
//	}
type ArchitectureCritique struct {
	ID           string                           `json:"id"`
	Status       string                           `json:"status"`
	StateSerial  int64                            `json:"state-serial"`
	RiskLevel    string                           `json:"risk-level,omitempty"`
	Architecture ArchitectureCritiqueArchitecture `json:"architecture"`
	Findings     []ArchitectureCritiqueFinding    `json:"findings,omitempty"`
	Deferred     []string                         `json:"deferred,omitempty"`

	// Telemetry / debugging
	Model        string `json:"model,omitempty"`
	InputTokens  int    `json:"input-tokens"`
	OutputTokens int    `json:"output-tokens"`
	ErrorMessage string `json:"error-message,omitempty"`

	CreatedAt string `json:"created-at,omitempty"`
	UpdatedAt string `json:"updated-at,omitempty"`
}

// ArchitectureCritiqueArchitecture is the model's inference of the system as it
// exists, grounded in resource addresses from state.
type ArchitectureCritiqueArchitecture struct {
	Summary     string   `json:"summary,omitempty"`
	Tiers       []string `json:"tiers,omitempty"`
	DataStores  []string `json:"data_stores,omitempty"`
	BlastRadius string   `json:"blast_radius,omitempty"`
}

// ArchitectureCritiqueFinding is one discrete critique item, anchored to a
// specific resource address and ranked by real operational risk.
type ArchitectureCritiqueFinding struct {
	Severity        string `json:"severity"`
	Category        string `json:"category"` // reliability|security|cost|operations|scalability
	Title           string `json:"title"`
	Detail          string `json:"detail"`
	ResourceAddress string `json:"resource_address"`
	Recommendation  string `json:"recommendation,omitempty"`
	// GroundedIn is the provenance: "state", a scanner rule id (e.g.
	// "CKV_AWS_24") for security findings, or "cost".
	GroundedIn string `json:"grounded_in,omitempty"`
}

// GetArchitectureCritique fetches the AI architecture critique for a
// workspace's CURRENT state version.
//
// workspaceID accepts either a bare workspace UUID or the prefixed "ws-<uuid>"
// form. Returns *NotFoundError when the feature is disabled, the workspace has
// no state, or no critique has been generated for the current state yet — the
// UI treats that as "absent" and offers to generate one.
func (c *Client) GetArchitectureCritique(
	ctx context.Context, workspaceID string,
) (*ArchitectureCritique, error) {
	if workspaceID == "" {
		return nil, errors.New("workspace id is required")
	}
	id := workspaceID
	if len(id) < 3 || id[:3] != "ws-" {
		id = "ws-" + id
	}
	data, err := c.Get(ctx, "/api/terrapod/v1/workspaces/"+url.PathEscape(id)+"/architecture-critique")
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse architecture critique response: %w", err)
	}
	return architectureCritiqueFromResource(res), nil
}

// RegenerateArchitectureCritique queues a fresh architecture critique for the
// workspace's current state. It mutates no infrastructure — it enqueues the
// async critic and returns a pending stub immediately. Listen for the
// architecture_critique_ready SSE event, or poll GetArchitectureCritique.
//
// Returns *NotFoundError when the ai_architecture feature is disabled.
func (c *Client) RegenerateArchitectureCritique(
	ctx context.Context, workspaceID string,
) (*ArchitectureCritique, error) {
	if workspaceID == "" {
		return nil, errors.New("workspace id is required")
	}
	id := workspaceID
	if len(id) < 3 || id[:3] != "ws-" {
		id = "ws-" + id
	}
	data, err := c.Post(
		ctx,
		"/api/terrapod/v1/workspaces/"+url.PathEscape(id)+"/architecture-critique/regenerate",
		nil,
	)
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse regenerate architecture critique response: %w", err)
	}
	return architectureCritiqueFromResource(res), nil
}

func architectureCritiqueFromResource(res *Resource) *ArchitectureCritique {
	cr := &ArchitectureCritique{
		ID:           res.ID,
		Status:       GetStringAttr(res, "status"),
		StateSerial:  GetIntAttr(res, "state-serial"),
		RiskLevel:    GetStringAttr(res, "risk-level"),
		Model:        GetStringAttr(res, "model"),
		InputTokens:  int(GetIntAttr(res, "input-tokens")),
		OutputTokens: int(GetIntAttr(res, "output-tokens")),
		ErrorMessage: GetStringAttr(res, "error-message"),
		CreatedAt:    GetStringAttr(res, "created-at"),
		UpdatedAt:    GetStringAttr(res, "updated-at"),
	}
	// architecture / findings / deferred arrive as nested JSON; unmarshal
	// directly rather than hand-walking interface values. The server produced
	// these against a fixed schema, so we don't re-validate here.
	if raw, ok := res.Attributes["architecture"]; ok {
		_ = json.Unmarshal(raw, &cr.Architecture)
	}
	if raw, ok := res.Attributes["findings"]; ok {
		_ = json.Unmarshal(raw, &cr.Findings)
	}
	if raw, ok := res.Attributes["deferred"]; ok {
		_ = json.Unmarshal(raw, &cr.Deferred)
	}
	return cr
}

// ArchitectureCritiqueMessage is one turn in the follow-up chat thread hanging
// off a workspace's current-state architecture critique (#1036). Role is "user"
// (an operator question) or "assistant" (the model's reply). Telemetry fields
// are meaningful only on assistant rows.
type ArchitectureCritiqueMessage struct {
	ID           string `json:"id"`
	Role         string `json:"role"`
	Content      string `json:"content"`
	Model        string `json:"model,omitempty"`
	InputTokens  int    `json:"input-tokens"`
	OutputTokens int    `json:"output-tokens"`
	ErrorMessage string `json:"error-message,omitempty"`
	CreatedAt    string `json:"created-at,omitempty"`
}

// ListArchitectureCritiqueMessages returns the follow-up chat thread for a
// workspace's current architecture critique, in chronological order (empty when
// no follow-ups have been posted). Returns *NotFoundError when the feature is
// disabled or there's no critique for the current state.
func (c *Client) ListArchitectureCritiqueMessages(
	ctx context.Context, workspaceID string,
) ([]ArchitectureCritiqueMessage, error) {
	if workspaceID == "" {
		return nil, errors.New("workspace id is required")
	}
	id := workspaceID
	if len(id) < 3 || id[:3] != "ws-" {
		id = "ws-" + id
	}
	data, err := c.Get(ctx, "/api/terrapod/v1/workspaces/"+url.PathEscape(id)+"/architecture-critique/messages")
	if err != nil {
		return nil, err
	}
	list, err := ParseResourceList(data)
	if err != nil {
		return nil, fmt.Errorf("parse architecture critique messages: %w", err)
	}
	msgs := make([]ArchitectureCritiqueMessage, 0, len(list))
	for i := range list {
		msgs = append(msgs, architectureCritiqueMessageFromResource(&list[i]))
	}
	return msgs, nil
}

// PostArchitectureCritiqueMessage posts one operator follow-up against the
// workspace's current critique and returns the assistant reply.
//
// Returns a *ConflictError when the per-critique message cap is reached, and the
// SDK's typed errors for the 429 (daily budget) / 503 (chat disabled) cases.
func (c *Client) PostArchitectureCritiqueMessage(
	ctx context.Context, workspaceID, content string,
) (*ArchitectureCritiqueMessage, error) {
	if workspaceID == "" {
		return nil, errors.New("workspace id is required")
	}
	if content == "" {
		return nil, errors.New("content is required")
	}
	id := workspaceID
	if len(id) < 3 || id[:3] != "ws-" {
		id = "ws-" + id
	}
	body, err := MarshalResource("architecture-critique-messages", map[string]any{"content": content}, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal message: %w", err)
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/workspaces/"+url.PathEscape(id)+"/architecture-critique/messages", body)
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse architecture critique message response: %w", err)
	}
	m := architectureCritiqueMessageFromResource(res)
	return &m, nil
}

func architectureCritiqueMessageFromResource(res *Resource) ArchitectureCritiqueMessage {
	return ArchitectureCritiqueMessage{
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
