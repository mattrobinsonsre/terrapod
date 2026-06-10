package terrapod

import (
	"context"
	"fmt"
	"net/url"
	"strconv"
)

// Workspace is the decoded form of one Terrapod workspace resource.
// Field tags mirror the JSON:API attribute names so callers can also
// (de)serialise directly when convenient.
//
// Nullable string fields (TerraformVersion, VCSRepoURL, VCSBranch,
// AgentPoolID, etc.) are returned as empty strings rather than nil
// pointers — Terrapod treats empty and null equivalently here, and the
// pointer-vs-empty distinction adds friction without information.
//
// DriftDetectionIntervalSeconds uses *int64 because zero is a
// meaningful value the server may return (disabled-by-explicit-zero
// vs. unset). Same logic for any future field where 0 is a legal
// non-default value.
type Workspace struct {
	ID                            string            `json:"id"`
	Name                          string            `json:"name"`
	ExecutionMode                 string            `json:"execution-mode"`
	ExecutionBackend              string            `json:"execution-backend,omitempty"`
	AutoApply                     bool              `json:"auto-apply"`
	TerraformVersion              string            `json:"terraform-version,omitempty"`
	WorkingDirectory              string            `json:"working-directory,omitempty"`
	ResourceCPU                   string            `json:"resource-cpu,omitempty"`
	ResourceMemory                string            `json:"resource-memory,omitempty"`
	VCSRepoURL                    string            `json:"vcs-repo-url,omitempty"`
	VCSBranch                     string            `json:"vcs-branch,omitempty"`
	VCSWorkflow                   string            `json:"vcs-workflow,omitempty"`
	VCSConnectionID               string            `json:"vcs-connection-id,omitempty"` // resolved from `vcs-connection` relationship
	AgentPoolID                   string            `json:"agent-pool-id,omitempty"`
	AutoMerge                     bool              `json:"auto-merge"`
	AutoMergeStrategy             string            `json:"auto-merge-strategy,omitempty"`
	OwnerEmail                    string            `json:"owner-email,omitempty"`
	Locked                        bool              `json:"locked"`
	Labels                        map[string]string `json:"labels,omitempty"`
	VarFiles                      []string          `json:"var-files,omitempty"`
	TriggerPrefixes               []string          `json:"trigger-prefixes,omitempty"`
	// DriftIgnoreRules is a list of resource-address-plus-attribute-path
	// glob patterns suppressed by the drift-result classifier (#482).
	// Empty list (default) means classic behaviour: every plan diff
	// counts as drift. See docs/api-reference.md for the rule grammar.
	DriftIgnoreRules              []string          `json:"drift-ignore-rules,omitempty"`
	DriftDetectionEnabled         bool              `json:"drift-detection-enabled"`
	DriftDetectionIntervalSeconds *int64            `json:"drift-detection-interval-seconds,omitempty"`
	DriftStatus                   string            `json:"drift-status,omitempty"`
	DriftLastCheckedAt            string            `json:"drift-last-checked-at,omitempty"`
	// DriftLatestRunID is the ID (prefixed `run-…`) of the drift run that
	// produced the current DriftStatus, or "" when drift has never run or
	// was just cleared by a successful apply. Lets consumers link the
	// status badge to the actual run.
	DriftLatestRunID string `json:"drift-latest-run-id,omitempty"`
	// StateDiverged is set when an apply Job succeeded but uploading the
	// resulting state to Terrapod failed — the workspace's recorded state
	// is now out of sync with reality.
	StateDiverged bool `json:"state-diverged"`
	// LifecycleState tracks autodiscovery-managed workspaces:
	// "active" | "pending_deletion" | "archived".
	LifecycleState string `json:"lifecycle-state,omitempty"`
	// LifecycleReason is a human-readable explanation of LifecycleState
	// (e.g. "directory 'accounts/x' removed on 'main'"). Empty for active.
	LifecycleReason string `json:"lifecycle-reason,omitempty"`
	// VCSLastPolledAt is the timestamp of the most recent successful VCS
	// poll cycle for this workspace.
	VCSLastPolledAt string `json:"vcs-last-polled-at,omitempty"`
	// VCSLastError is the last error from a VCS poll attempt (auth
	// failure, repo gone, etc.). Empty when the last poll succeeded.
	VCSLastError string `json:"vcs-last-error,omitempty"`
	// VCSLastErrorAt is the timestamp of VCSLastError.
	VCSLastErrorAt string `json:"vcs-last-error-at,omitempty"`
	// AgentPoolName is the human-readable name of the assigned agent
	// pool, server-derived from AgentPoolID. Empty when no pool is set.
	AgentPoolName string `json:"agent-pool-name,omitempty"`
	// VCSConnectionName is the human-readable name of the assigned VCS
	// connection, server-derived from VCSConnectionID. Empty when none.
	VCSConnectionName string `json:"vcs-connection-name,omitempty"`
	// AISummaryMode is the three-state per-workspace override (#401):
	//   "default"  → follow the deployment-wide ai_summary.enabled flag
	//   "enabled"  → always summarise (no-op when global is off)
	//   "disabled" → never summarise this workspace's plans
	AISummaryMode string `json:"ai-summary-mode,omitempty"`
	// AISummaryContext is workspace-specific facts added on top of the
	// deployment-wide fleet_context when the summariser builds its prompt.
	AISummaryContext string `json:"ai-summary-context,omitempty"`
	CreatedAt        string `json:"created-at,omitempty"`
	UpdatedAt        string `json:"updated-at,omitempty"`
}

// CreateWorkspaceRequest is the input shape for Client.CreateWorkspace.
// Only Name is required by Terrapod; every other field is optional and
// either takes a server-side default or is updateable later.
//
// To produce a JSON:API body the SDK marshals from this struct rather
// than a free-form map so callers get type safety and the Terrapod
// schema stays singular-sourced in this file.
type CreateWorkspaceRequest struct {
	Name                          string             `json:"name"`
	ExecutionMode                 string             `json:"execution-mode,omitempty"`
	ExecutionBackend              string             `json:"execution-backend,omitempty"`
	AutoApply                     *bool              `json:"auto-apply,omitempty"`
	TerraformVersion              string             `json:"terraform-version,omitempty"`
	WorkingDirectory              string             `json:"working-directory,omitempty"`
	ResourceCPU                   string             `json:"resource-cpu,omitempty"`
	ResourceMemory                string             `json:"resource-memory,omitempty"`
	VCSRepoURL                    string             `json:"vcs-repo-url,omitempty"`
	VCSBranch                     string             `json:"vcs-branch,omitempty"`
	VCSWorkflow                   string             `json:"vcs-workflow,omitempty"`
	VCSConnectionID               string             `json:"-"` // → relationship, not attribute
	AgentPoolID                   string             `json:"agent-pool-id,omitempty"`
	AutoMerge                     *bool              `json:"auto-merge,omitempty"`
	AutoMergeStrategy             string             `json:"auto-merge-strategy,omitempty"`
	OwnerEmail                    string             `json:"owner-email,omitempty"`
	Labels                        map[string]string  `json:"labels,omitempty"`
	VarFiles                      []string           `json:"var-files,omitempty"`
	TriggerPrefixes               []string           `json:"trigger-prefixes,omitempty"`
	DriftIgnoreRules              []string           `json:"drift-ignore-rules,omitempty"`
	DriftDetectionEnabled         *bool              `json:"drift-detection-enabled,omitempty"`
	DriftDetectionIntervalSeconds *int64             `json:"drift-detection-interval-seconds,omitempty"`
	// AISummaryMode is the three-state per-workspace override (#401):
	// "default" | "enabled" | "disabled". Empty string omits the field
	// (server-side default applies — "default").
	AISummaryMode string `json:"ai-summary-mode,omitempty"`
	// AISummaryContext is workspace-specific context added to the model
	// prompt. Capped at 4000 chars server-side.
	AISummaryContext string `json:"ai-summary-context,omitempty"`
}

// UpdateWorkspaceRequest is the input shape for Client.UpdateWorkspace.
// Mirrors CreateWorkspaceRequest including Name — Terrapod's
// workspace PATCH endpoint accepts a rename, so the SDK exposes it
// here. Pass an empty string to leave the name alone.
//
// Pointer-typed bool fields let callers distinguish "leave alone"
// (nil) from "set to false" (&false). Without that distinction we'd
// flip a workspace's auto-apply to false on every PATCH that didn't
// explicitly set it.
type UpdateWorkspaceRequest struct {
	Name                          string            `json:"name,omitempty"`
	ExecutionMode                 string            `json:"execution-mode,omitempty"`
	ExecutionBackend              string            `json:"execution-backend,omitempty"`
	AutoApply                     *bool             `json:"auto-apply,omitempty"`
	TerraformVersion              string            `json:"terraform-version,omitempty"`
	WorkingDirectory              string            `json:"working-directory,omitempty"`
	ResourceCPU                   string            `json:"resource-cpu,omitempty"`
	ResourceMemory                string            `json:"resource-memory,omitempty"`
	VCSRepoURL                    string            `json:"vcs-repo-url,omitempty"`
	VCSBranch                     string            `json:"vcs-branch,omitempty"`
	VCSWorkflow                   string            `json:"vcs-workflow,omitempty"`
	VCSConnectionID               string            `json:"-"`
	AgentPoolID                   string            `json:"agent-pool-id,omitempty"`
	AutoMerge                     *bool             `json:"auto-merge,omitempty"`
	AutoMergeStrategy             string            `json:"auto-merge-strategy,omitempty"`
	Labels                        map[string]string `json:"labels,omitempty"`
	VarFiles                      []string          `json:"var-files,omitempty"`
	TriggerPrefixes               []string          `json:"trigger-prefixes,omitempty"`
	DriftIgnoreRules              []string          `json:"drift-ignore-rules,omitempty"`
	DriftDetectionEnabled         *bool             `json:"drift-detection-enabled,omitempty"`
	DriftDetectionIntervalSeconds *int64            `json:"drift-detection-interval-seconds,omitempty"`
	// AISummaryMode see CreateWorkspaceRequest. On UPDATE, empty string
	// leaves the existing value untouched — to explicitly set "follow
	// deployment default", pass "default".
	AISummaryMode string `json:"ai-summary-mode,omitempty"`
	// AISummaryContext see CreateWorkspaceRequest. To clear an existing
	// context, set this to "" — but note empty string also means
	// "leave alone" (a Terrapod-side limitation; clear via the UI).
	AISummaryContext *string `json:"ai-summary-context,omitempty"`
}

// WorkspaceListOptions filters and paginates ListWorkspaces. Zero
// values produce a default-paged unfiltered list.
type WorkspaceListOptions struct {
	// PageNumber is 1-indexed (matches the server). 0 means "page 1".
	PageNumber int
	// PageSize defaults to the server's default (20) when 0.
	PageSize int
	// Search matches workspace names with prefix/substring semantics
	// (server-controlled). Empty means no name filter.
	Search string
}

// WorkspaceList is the paginated result of ListWorkspaces.
type WorkspaceList struct {
	Items       []Workspace
	CurrentPage int
	TotalPages  int
	TotalCount  int
}

// CreateWorkspace creates a new workspace in the default organisation
// (Terrapod is single-org by design). Returns the created Workspace
// with server-populated fields filled in (id, created_at, etc.).
//
// Common errors:
//   - *ConflictError when the name is already taken
//   - *ValidationError on invalid attribute values
//   - *AuthorizationError when the token lacks `admin` on the workspace
func (c *Client) CreateWorkspace(ctx context.Context, req CreateWorkspaceRequest) (*Workspace, error) {
	attrs := workspaceCreateAttrs(req)
	rels := workspaceRels(req.VCSConnectionID)
	body, err := MarshalResource("workspaces", attrs, rels)
	if err != nil {
		return nil, fmt.Errorf("marshal create workspace: %w", err)
	}
	data, err := c.Post(ctx, "/api/v2/organizations/default/workspaces", body)
	if err != nil {
		return nil, err
	}
	return parseWorkspace(data)
}

// GetWorkspace reads a workspace by id ("ws-..."). Returns
// *NotFoundError when the id is unknown.
func (c *Client) GetWorkspace(ctx context.Context, id string) (*Workspace, error) {
	data, err := c.Get(ctx, "/api/v2/workspaces/"+url.PathEscape(id))
	if err != nil {
		return nil, err
	}
	return parseWorkspace(data)
}

// GetWorkspaceByName reads a workspace by its name (the human-
// recognisable identifier operators use). Useful for import flows
// where the operator types the name they see in the UI rather than
// the typed-id form.
func (c *Client) GetWorkspaceByName(ctx context.Context, name string) (*Workspace, error) {
	data, err := c.Get(ctx, "/api/v2/organizations/default/workspaces/"+url.PathEscape(name))
	if err != nil {
		return nil, err
	}
	return parseWorkspace(data)
}

// UpdateWorkspace partially updates the workspace identified by id.
// Fields whose zero value should be a no-op (don't set this field on
// the server) use pointer types; setting a nil pointer leaves the
// server-side value unchanged.
//
// Returns the updated Workspace.
func (c *Client) UpdateWorkspace(ctx context.Context, id string, req UpdateWorkspaceRequest) (*Workspace, error) {
	attrs := workspaceUpdateAttrs(req)
	rels := workspaceRels(req.VCSConnectionID)
	body, err := MarshalResourceWithIDAndRels(id, "workspaces", attrs, rels)
	if err != nil {
		return nil, fmt.Errorf("marshal update workspace: %w", err)
	}
	data, err := c.Patch(ctx, "/api/v2/workspaces/"+url.PathEscape(id), body)
	if err != nil {
		return nil, err
	}
	return parseWorkspace(data)
}

// DeleteWorkspace removes the workspace identified by id. The endpoint
// lives on the Terrapod-native prefix (not /api/v2/) — workspace
// deletion isn't a CLI-consumed surface and the v2 alias was removed
// in #278.
//
// Returns nil on success, *NotFoundError if the workspace doesn't
// exist (treated as success by most callers — idempotent delete).
func (c *Client) DeleteWorkspace(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/terrapod/v1/workspaces/"+url.PathEscape(id))
}

// ListWorkspaces returns one page of workspaces. The caller drives
// pagination explicitly via opts.PageNumber so a large org with
// thousands of workspaces still finishes (the alternative —
// auto-pagination inside this method — would block for minutes).
func (c *Client) ListWorkspaces(ctx context.Context, opts WorkspaceListOptions) (*WorkspaceList, error) {
	q := url.Values{}
	if opts.PageNumber > 0 {
		q.Set("page[number]", strconv.Itoa(opts.PageNumber))
	}
	if opts.PageSize > 0 {
		q.Set("page[size]", strconv.Itoa(opts.PageSize))
	}
	if opts.Search != "" {
		q.Set("search[name]", opts.Search)
	}
	path := "/api/v2/organizations/default/workspaces"
	if encoded := q.Encode(); encoded != "" {
		path += "?" + encoded
	}
	data, err := c.Get(ctx, path)
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, err
	}
	list := &WorkspaceList{Items: make([]Workspace, 0, len(resources))}
	for i := range resources {
		ws := workspaceFromResource(&resources[i])
		list.Items = append(list.Items, *ws)
	}
	// Pagination meta is in the `meta` block of the document, decoded
	// separately so the resource-list parse stays clean.
	if meta, err := parseListMeta(data); err == nil {
		list.CurrentPage = meta.CurrentPage
		list.TotalPages = meta.TotalPages
		list.TotalCount = meta.TotalCount
	}
	return list, nil
}

// ── Internal helpers ─────────────────────────────────────────────────

// workspaceCreateAttrs turns a CreateWorkspaceRequest into the JSON:API
// attributes map. Only set keys go in — Terrapod doesn't tolerate
// `null` for fields it expects to omit, and `omitempty` on struct
// tags doesn't help with nested map marshaling.
func workspaceCreateAttrs(req CreateWorkspaceRequest) map[string]any {
	attrs := map[string]any{"name": req.Name}
	if req.ExecutionMode != "" {
		attrs["execution-mode"] = req.ExecutionMode
	}
	if req.ExecutionBackend != "" {
		attrs["execution-backend"] = req.ExecutionBackend
	}
	if req.AutoApply != nil {
		attrs["auto-apply"] = *req.AutoApply
	}
	if req.TerraformVersion != "" {
		attrs["terraform-version"] = req.TerraformVersion
	}
	if req.WorkingDirectory != "" {
		attrs["working-directory"] = req.WorkingDirectory
	}
	if req.ResourceCPU != "" {
		attrs["resource-cpu"] = req.ResourceCPU
	}
	if req.ResourceMemory != "" {
		attrs["resource-memory"] = req.ResourceMemory
	}
	if req.VCSRepoURL != "" {
		attrs["vcs-repo-url"] = req.VCSRepoURL
	}
	if req.VCSBranch != "" {
		attrs["vcs-branch"] = req.VCSBranch
	}
	if req.VCSWorkflow != "" {
		attrs["vcs-workflow"] = req.VCSWorkflow
	}
	if req.AgentPoolID != "" {
		attrs["agent-pool-id"] = req.AgentPoolID
	}
	if req.AutoMerge != nil {
		attrs["auto-merge"] = *req.AutoMerge
	}
	if req.AutoMergeStrategy != "" {
		attrs["auto-merge-strategy"] = req.AutoMergeStrategy
	}
	if req.OwnerEmail != "" {
		attrs["owner-email"] = req.OwnerEmail
	}
	if req.Labels != nil {
		attrs["labels"] = req.Labels
	}
	if req.VarFiles != nil {
		attrs["var-files"] = req.VarFiles
	}
	if req.TriggerPrefixes != nil {
		attrs["trigger-prefixes"] = req.TriggerPrefixes
	}
	if req.DriftIgnoreRules != nil {
		attrs["drift-ignore-rules"] = req.DriftIgnoreRules
	}
	if req.DriftDetectionEnabled != nil {
		attrs["drift-detection-enabled"] = *req.DriftDetectionEnabled
	}
	if req.DriftDetectionIntervalSeconds != nil {
		attrs["drift-detection-interval-seconds"] = *req.DriftDetectionIntervalSeconds
	}
	if req.AISummaryMode != "" {
		attrs["ai-summary-mode"] = req.AISummaryMode
	}
	if req.AISummaryContext != "" {
		attrs["ai-summary-context"] = req.AISummaryContext
	}
	return attrs
}

// workspaceUpdateAttrs mirrors workspaceCreateAttrs minus OwnerEmail
// (which only platform admins can change, via a separate endpoint).
// Name is included so PATCH can rename a workspace; the empty-string
// check on every field is what makes the "leave alone" semantics work
// on every PATCH.
func workspaceUpdateAttrs(req UpdateWorkspaceRequest) map[string]any {
	attrs := map[string]any{}
	if req.Name != "" {
		attrs["name"] = req.Name
	}
	if req.ExecutionMode != "" {
		attrs["execution-mode"] = req.ExecutionMode
	}
	if req.ExecutionBackend != "" {
		attrs["execution-backend"] = req.ExecutionBackend
	}
	if req.AutoApply != nil {
		attrs["auto-apply"] = *req.AutoApply
	}
	if req.TerraformVersion != "" {
		attrs["terraform-version"] = req.TerraformVersion
	}
	if req.WorkingDirectory != "" {
		attrs["working-directory"] = req.WorkingDirectory
	}
	if req.ResourceCPU != "" {
		attrs["resource-cpu"] = req.ResourceCPU
	}
	if req.ResourceMemory != "" {
		attrs["resource-memory"] = req.ResourceMemory
	}
	if req.VCSRepoURL != "" {
		attrs["vcs-repo-url"] = req.VCSRepoURL
	}
	if req.VCSBranch != "" {
		attrs["vcs-branch"] = req.VCSBranch
	}
	if req.VCSWorkflow != "" {
		attrs["vcs-workflow"] = req.VCSWorkflow
	}
	if req.AgentPoolID != "" {
		attrs["agent-pool-id"] = req.AgentPoolID
	}
	if req.AutoMerge != nil {
		attrs["auto-merge"] = *req.AutoMerge
	}
	if req.AutoMergeStrategy != "" {
		attrs["auto-merge-strategy"] = req.AutoMergeStrategy
	}
	if req.Labels != nil {
		attrs["labels"] = req.Labels
	}
	if req.VarFiles != nil {
		attrs["var-files"] = req.VarFiles
	}
	if req.TriggerPrefixes != nil {
		attrs["trigger-prefixes"] = req.TriggerPrefixes
	}
	if req.DriftIgnoreRules != nil {
		attrs["drift-ignore-rules"] = req.DriftIgnoreRules
	}
	if req.DriftDetectionEnabled != nil {
		attrs["drift-detection-enabled"] = *req.DriftDetectionEnabled
	}
	if req.DriftDetectionIntervalSeconds != nil {
		attrs["drift-detection-interval-seconds"] = *req.DriftDetectionIntervalSeconds
	}
	if req.AISummaryMode != "" {
		attrs["ai-summary-mode"] = req.AISummaryMode
	}
	if req.AISummaryContext != nil {
		// *string so callers can explicitly clear the context with &"".
		attrs["ai-summary-context"] = *req.AISummaryContext
	}
	return attrs
}

// workspaceRels builds the relationships block for create + update.
// Empty vcsConnectionID returns nil — caller's MarshalResource handles
// that by omitting the `relationships` key entirely.
func workspaceRels(vcsConnectionID string) map[string]any {
	if vcsConnectionID == "" {
		return nil
	}
	return map[string]any{
		"vcs-connection": map[string]any{
			"data": map[string]any{
				"id":   vcsConnectionID,
				"type": "vcs-connections",
			},
		},
	}
}

// parseWorkspace decodes a JSON:API single-workspace response.
func parseWorkspace(body []byte) (*Workspace, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse workspace response: %w", err)
	}
	return workspaceFromResource(res), nil
}

// workspaceFromResource projects the raw Resource into a typed Workspace.
// Centralised so the list-decoding path uses the same projection as
// the single-resource path.
func workspaceFromResource(res *Resource) *Workspace {
	ws := &Workspace{
		ID:                    res.ID,
		Name:                  GetStringAttr(res, "name"),
		ExecutionMode:         GetStringAttr(res, "execution-mode"),
		ExecutionBackend:      GetStringAttr(res, "execution-backend"),
		AutoApply:             GetBoolAttr(res, "auto-apply"),
		TerraformVersion:      GetStringAttr(res, "terraform-version"),
		WorkingDirectory:      GetStringAttr(res, "working-directory"),
		ResourceCPU:           GetStringAttr(res, "resource-cpu"),
		ResourceMemory:        GetStringAttr(res, "resource-memory"),
		VCSRepoURL:            GetStringAttr(res, "vcs-repo-url"),
		VCSBranch:             GetStringAttr(res, "vcs-branch"),
		VCSWorkflow:           GetStringAttr(res, "vcs-workflow"),
		VCSConnectionID:       GetRelationshipID(res, "vcs-connection"),
		AgentPoolID:           GetStringAttr(res, "agent-pool-id"),
		AutoMerge:             GetBoolAttr(res, "auto-merge"),
		AutoMergeStrategy:     GetStringAttr(res, "auto-merge-strategy"),
		OwnerEmail:            GetStringAttr(res, "owner-email"),
		Locked:                GetBoolAttr(res, "locked"),
		Labels:                GetMapAttr(res, "labels"),
		VarFiles:              GetListAttr(res, "var-files"),
		TriggerPrefixes:       GetListAttr(res, "trigger-prefixes"),
		DriftIgnoreRules:      GetListAttr(res, "drift-ignore-rules"),
		DriftDetectionEnabled: GetBoolAttr(res, "drift-detection-enabled"),
		DriftStatus:           GetStringAttr(res, "drift-status"),
		DriftLastCheckedAt:    GetStringAttr(res, "drift-last-checked-at"),
		DriftLatestRunID:      GetStringAttr(res, "drift-latest-run-id"),
		StateDiverged:         GetBoolAttr(res, "state-diverged"),
		LifecycleState:        GetStringAttr(res, "lifecycle-state"),
		LifecycleReason:       GetStringAttr(res, "lifecycle-reason"),
		VCSLastPolledAt:       GetStringAttr(res, "vcs-last-polled-at"),
		VCSLastError:          GetStringAttr(res, "vcs-last-error"),
		VCSLastErrorAt:        GetStringAttr(res, "vcs-last-error-at"),
		AgentPoolName:         GetStringAttr(res, "agent-pool-name"),
		VCSConnectionName:     GetStringAttr(res, "vcs-connection-name"),
		AISummaryMode:         GetStringAttr(res, "ai-summary-mode"),
		AISummaryContext:      GetStringAttr(res, "ai-summary-context"),
		CreatedAt:             GetStringAttr(res, "created-at"),
		UpdatedAt:             GetStringAttr(res, "updated-at"),
	}
	if v := GetIntAttr(res, "drift-detection-interval-seconds"); v > 0 {
		ws.DriftDetectionIntervalSeconds = &v
	}
	return ws
}
