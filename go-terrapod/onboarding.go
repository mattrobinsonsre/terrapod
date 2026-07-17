package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
)

// OnboardingAvailability reports whether the optional AI onboarding mode is
// available. Onboarding itself has no feature flag — it is gated per workspace
// by the workspace:onboard capability — so this probe only describes the AI
// path (its own switch + a configured model).
type OnboardingAvailability struct {
	AIAvailable       bool `json:"ai-available"`
	AIModelConfigured bool `json:"ai-model-configured"`
}

// OnboardingDataSource is one entry in a session's discovery surface: a data
// source the deterministic import path can consume (it exposes a derivable
// resource-id list and singularises to a real managed resource). The field tags
// mirror the snake_case JSON emitted by `terrapod-query schema --importable` and
// cached server-side, NOT the kebab-case JSON:API attribute convention used
// elsewhere in this SDK.
type OnboardingDataSource struct {
	Provider       string   `json:"provider"`
	Name           string   `json:"name"`
	HasFilter      bool     `json:"has_filter"`
	HasTags        bool     `json:"has_tags"`
	ReturnsList    bool     `json:"returns_list"`
	IDListAttr     string   `json:"id_list_attr,omitempty"`
	ResourceType   string   `json:"resource_type,omitempty"`
	Inputs         []string `json:"inputs,omitempty"`
	RequiredInputs []string `json:"required_inputs,omitempty"`
}

// DiscoverySurface is the credential-less D1 schema-discovery result for a
// session: the importable data sources the caller may query. Present on the
// detail read (GetOnboardingSession / StartOnboardingDiscovery) once the session
// reaches schema_ready; nil on the list read (the surface is a large per-session
// value the list endpoint omits).
type DiscoverySurface struct {
	Count       int                    `json:"count"`
	DataSources []OnboardingDataSource `json:"data_sources"`
}

// OnboardingSession is the decoded form of one onboarding / resource-discovery
// session. Field tags mirror the JSON:API attribute names.
//
// Nullable value fields use pointers where nil is meaningful: DataSourceCount
// and DiscoverySurface are nil until D1 schema discovery completes (and stay nil
// on the list read, which omits the surface). Config fields are empty strings
// until the D2/D3 discovery run produces them.
type OnboardingSession struct {
	ID              string   `json:"id"`
	WorkspaceID     string   `json:"workspace-id"`
	Status          string   `json:"status"`
	Provider        string   `json:"provider"`
	ProviderVersion string   `json:"provider-version,omitempty"`
	Engine          string   `json:"engine,omitempty"`
	EngineVersion   string   `json:"engine-version,omitempty"`
	SelectedTypes   []string `json:"selected-types,omitempty"`
	AIAssisted      bool     `json:"ai-assisted"`
	Error           string   `json:"error,omitempty"`
	// DataSourceCount is the number of importable data sources the surface
	// found; nil until schema discovery completes (and on the list read).
	DataSourceCount *int64 `json:"data-source-count,omitempty"`
	// DiscoverySurface is the full importable-data-source catalogue; nil on the
	// list read and until schema_ready (see DiscoverySurface).
	DiscoverySurface *DiscoverySurface `json:"discovery-surface,omitempty"`
	// GeneratedConfig and ImportBlocks are the reviewable D3 output — the
	// resource config and the matching import {} blocks. Empty until the
	// session reaches config_ready.
	GeneratedConfig string `json:"generated-config,omitempty"`
	ImportBlocks    string `json:"import-blocks,omitempty"`
	// PolishedConfig / PolishedImportBlocks are the optional AI-polished view
	// (resources renamed from tags, grouped, commented; import ids untouched).
	// Empty until the polish lands (or if rejected / AI disabled). AIAssisted is
	// true iff these are populated.
	PolishedConfig       string `json:"polished-config,omitempty"`
	PolishedImportBlocks string `json:"polished-import-blocks,omitempty"`
	// PairedConfig / PairedPolishedConfig are the presentation-only view with
	// each import {} block interleaved above the resource it targets. Computed
	// server-side at serialize time; empty until config exists.
	PairedConfig         string `json:"paired-config,omitempty"`
	PairedPolishedConfig string `json:"paired-polished-config,omitempty"`
	// DiscoveryRunID is the D2/D3 discovery run; ResultRunID is the eventual
	// import-only run. Empty until each is dispatched.
	DiscoveryRunID string `json:"discovery-run-id,omitempty"`
	ResultRunID    string `json:"result-run-id,omitempty"`
	CreatedBy      string `json:"created-by,omitempty"`
	CreatedAt      string `json:"created-at,omitempty"`
	UpdatedAt      string `json:"updated-at,omitempty"`
}

// CreateOnboardingSessionRequest starts a discovery session for a workspace and
// kicks off credential-less D1 schema discovery. Provider is a lowercase
// terraform provider name (e.g. "aws"); ProviderVersion is an optional terraform
// version constraint (e.g. "~> 5.0", "< 6.0"). Both are validated server-side
// (422 on bad input).
type CreateOnboardingSessionRequest struct {
	WorkspaceID     string
	Provider        string
	ProviderVersion string
}

// GetOnboardingAvailability reports whether the optional AI onboarding mode is
// available (any authenticated user).
func (c *Client) GetOnboardingAvailability(ctx context.Context) (*OnboardingAvailability, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/onboarding")
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse onboarding availability response: %w", err)
	}
	return &OnboardingAvailability{
		AIAvailable:       GetBoolAttr(res, "ai-available"),
		AIModelConfigured: GetBoolAttr(res, "ai-model-configured"),
	}, nil
}

// CreateOnboardingSession starts a discovery session and kicks off D1 schema
// discovery off the request thread. Poll GetOnboardingSession until Status ==
// "schema_ready", then read DiscoverySurface. Requires the workspace:onboard
// capability on the workspace.
func (c *Client) CreateOnboardingSession(ctx context.Context, req CreateOnboardingSessionRequest) (*OnboardingSession, error) {
	if req.WorkspaceID == "" {
		return nil, fmt.Errorf("workspace id is required")
	}
	attrs := map[string]any{"provider": req.Provider}
	if req.ProviderVersion != "" {
		attrs["provider-version"] = req.ProviderVersion
	}
	body, err := MarshalResource("onboarding-sessions", attrs, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create onboarding-session: %w", err)
	}
	data, err := c.Post(ctx,
		fmt.Sprintf("/api/terrapod/v1/workspaces/%s/onboarding-sessions", url.PathEscape(req.WorkspaceID)),
		body)
	if err != nil {
		return nil, err
	}
	return parseOnboardingSession(data)
}

// ListOnboardingSessions returns a workspace's discovery sessions, newest-ish
// first (server order). The discovery surface is omitted from list entries; read
// a single session for it. Requires workspace:onboard.
func (c *Client) ListOnboardingSessions(ctx context.Context, workspaceID string) ([]OnboardingSession, error) {
	if workspaceID == "" {
		return nil, fmt.Errorf("workspace id is required")
	}
	data, err := c.Get(ctx,
		fmt.Sprintf("/api/terrapod/v1/workspaces/%s/onboarding-sessions", url.PathEscape(workspaceID)))
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, fmt.Errorf("parse onboarding-sessions list: %w", err)
	}
	out := make([]OnboardingSession, 0, len(resources))
	for i := range resources {
		out = append(out, *onboardingSessionFromResource(&resources[i]))
	}
	return out, nil
}

// GetOnboardingSession reads one session by id, including the discovery surface
// (from the time-limited server cache; an expired cache simply re-runs
// discovery). Requires workspace:onboard on the session's workspace.
func (c *Client) GetOnboardingSession(ctx context.Context, sessionID string) (*OnboardingSession, error) {
	if sessionID == "" {
		return nil, fmt.Errorf("session id is required")
	}
	data, err := c.Get(ctx, "/api/terrapod/v1/onboarding-sessions/"+url.PathEscape(sessionID))
	if err != nil {
		return nil, err
	}
	return parseOnboardingSession(data)
}

// StartOnboardingDiscovery dispatches the D2/D3 runner discovery run for a
// schema_ready session. selectedTypes is the subset of the session's discovery
// surface (data-source types) to query — it must be a non-empty subset, else the
// server returns 422. Requires workspace:onboard.
func (c *Client) StartOnboardingDiscovery(ctx context.Context, sessionID string, selectedTypes []string) (*OnboardingSession, error) {
	if sessionID == "" {
		return nil, fmt.Errorf("session id is required")
	}
	body, err := MarshalResource("onboarding-sessions", map[string]any{
		"selected-types": selectedTypes,
	}, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal start onboarding-discovery: %w", err)
	}
	data, err := c.Post(ctx,
		fmt.Sprintf("/api/terrapod/v1/onboarding-sessions/%s/discover", url.PathEscape(sessionID)),
		body)
	if err != nil {
		return nil, err
	}
	return parseOnboardingSession(data)
}

// ── Internal helpers ─────────────────────────────────────────────────

func parseOnboardingSession(body []byte) (*OnboardingSession, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse onboarding-session response: %w", err)
	}
	return onboardingSessionFromResource(res), nil
}

func onboardingSessionFromResource(res *Resource) *OnboardingSession {
	s := &OnboardingSession{
		ID:                   res.ID,
		WorkspaceID:          GetStringAttr(res, "workspace-id"),
		Status:               GetStringAttr(res, "status"),
		Provider:             GetStringAttr(res, "provider"),
		ProviderVersion:      GetStringAttr(res, "provider-version"),
		Engine:               GetStringAttr(res, "engine"),
		EngineVersion:        GetStringAttr(res, "engine-version"),
		SelectedTypes:        GetListAttr(res, "selected-types"),
		AIAssisted:           GetBoolAttr(res, "ai-assisted"),
		Error:                GetStringAttr(res, "error"),
		GeneratedConfig:      GetStringAttr(res, "generated-config"),
		ImportBlocks:         GetStringAttr(res, "import-blocks"),
		PolishedConfig:       GetStringAttr(res, "polished-config"),
		PolishedImportBlocks: GetStringAttr(res, "polished-import-blocks"),
		PairedConfig:         GetStringAttr(res, "paired-config"),
		PairedPolishedConfig: GetStringAttr(res, "paired-polished-config"),
		DiscoveryRunID:       GetStringAttr(res, "discovery-run-id"),
		ResultRunID:          GetStringAttr(res, "result-run-id"),
		CreatedBy:            GetStringAttr(res, "created-by"),
		CreatedAt:            GetStringAttr(res, "created-at"),
		UpdatedAt:            GetStringAttr(res, "updated-at"),
	}
	// Nullable count: present (non-null) only once schema discovery completes.
	if raw, ok := res.Attributes["data-source-count"]; ok && len(raw) > 0 && string(raw) != "null" {
		var n int64
		if json.Unmarshal(raw, &n) == nil {
			s.DataSourceCount = &n
		}
	}
	// Nested surface object: present only on the detail read once schema_ready.
	if raw, ok := res.Attributes["discovery-surface"]; ok && len(raw) > 0 && string(raw) != "null" {
		var surf DiscoverySurface
		if json.Unmarshal(raw, &surf) == nil {
			s.DiscoverySurface = &surf
		}
	}
	return s
}
