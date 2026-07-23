package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"strconv"
	"strings"
)

// RunActions reports which lifecycle transitions a run currently permits. It
// mirrors the server's `actions` attribute block — the same signals the UI keys
// its buttons off — so an agent can decide what it may do next without
// re-deriving it from Status.
type RunActions struct {
	IsConfirmable bool `json:"is-confirmable"`
	IsDiscardable bool `json:"is-discardable"`
	IsCancelable  bool `json:"is-cancelable"`
	IsRetryable   bool `json:"is-retryable"`
}

// RunStatusTimestamps records when the run entered each phase (RFC3339 with a
// trailing Z, or empty if not reached). Useful for an agent narrating progress.
type RunStatusTimestamps struct {
	PlanQueuedAt string `json:"plan-queued-at,omitempty"`
	PlanningAt   string `json:"planning-at,omitempty"`
	PlannedAt    string `json:"planned-at,omitempty"`
	ApplyingAt   string `json:"applying-at,omitempty"`
	AppliedAt    string `json:"applied-at,omitempty"`
}

// Run is the decoded form of one Terrapod run. Field tags mirror the JSON:API
// attribute names.
//
// The struct deliberately carries Terrapod-native fields that a generic TFE
// client does not model — HasJSONOutput, HasChanges, IsDriftDetection, the
// resource profile (PeakMemoryBytes / RunnerExit*), Terragrunt*, and
// StateDiverged — because the whole point of the native SDK is to surface what
// Terrapod actually reports about a run.
//
// Nullable fields use pointers where nil is meaningful and distinct from the
// zero value: HasChanges (unknown until planned), PeakMemoryBytes / PeakCPUUsec /
// RunnerExitCode (unset until the Job reports a profile), and
// VCSPullRequestNumber (nil for branch/CLI runs).
type Run struct {
	ID               string `json:"id"`
	Status           string `json:"status"`
	Message          string `json:"message,omitempty"`
	DiscardReason    string `json:"discard-reason,omitempty"`
	IsDestroy        bool   `json:"is-destroy"`
	AutoApply        bool   `json:"auto-apply"`
	PlanOnly         bool   `json:"plan-only"`
	Source           string `json:"source,omitempty"`
	ExecutionBackend string `json:"execution-backend,omitempty"`
	TerraformVersion string `json:"terraform-version,omitempty"`
	// Terragrunt* echo the workspace's terragrunt wrapping for this run.
	TerragruntEnabled bool   `json:"terragrunt-enabled"`
	TerragruntVersion string `json:"terragrunt-version,omitempty"`
	ResourceCPU       string `json:"resource-cpu,omitempty"`
	ResourceMemory    string `json:"resource-memory,omitempty"`
	// Resource profile — populated once the Job reports it. Nil until then.
	PeakMemoryBytes  *int64 `json:"peak-memory-bytes,omitempty"`
	PeakCPUUsec      *int64 `json:"peak-cpu-usec,omitempty"`
	RunnerExitCode   *int64 `json:"runner-exit-code,omitempty"`
	RunnerExitReason string `json:"runner-exit-reason,omitempty"`
	RunnerExitStatus string `json:"runner-exit-status,omitempty"`
	ErrorMessage     string `json:"error-message,omitempty"`
	// Run options this run was created with (echoed back).
	TargetAddrs     []string `json:"target-addrs,omitempty"`
	ReplaceAddrs    []string `json:"replace-addrs,omitempty"`
	RefreshOnly     bool     `json:"refresh-only"`
	Refresh         bool     `json:"refresh"`
	AllowEmptyApply bool     `json:"allow-empty-apply"`
	// Terrapod-native observability the MCP most needs.
	IsDriftDetection bool  `json:"is-drift-detection"`
	HasChanges       *bool `json:"has-changes,omitempty"`
	HasJSONOutput    bool  `json:"has-json-output"`
	StateDiverged    bool  `json:"state-diverged"`
	// Cost estimation (#871): HasCostEstimate gates the run's Cost tab; the cached
	// monthly range is echoed for cheap list display. Currency/min/max are nil
	// until an estimate exists (a plan-only or errored run may have none).
	HasCostEstimate bool     `json:"has-cost-estimate"`
	CostCurrency    string   `json:"cost-currency,omitempty"`
	CostMonthlyMin  *float64 `json:"cost-monthly-min,omitempty"`
	CostMonthlyMax  *float64 `json:"cost-monthly-max,omitempty"`
	// Workspace context.
	WorkspaceID   string `json:"workspace-id,omitempty"` // from the `workspace` relationship
	WorkspaceName string `json:"workspace-name,omitempty"`
	// VCS metadata (empty / nil for CLI runs).
	VCSCommitSHA         string `json:"vcs-commit-sha,omitempty"`
	VCSBranch            string `json:"vcs-branch,omitempty"`
	VCSPullRequestNumber *int64 `json:"vcs-pull-request-number,omitempty"`
	CreatedBy            string `json:"created-by,omitempty"`
	CreatedAt            string `json:"created-at,omitempty"`
	UpdatedAt            string `json:"updated-at,omitempty"`
	// Nested blocks.
	Actions          RunActions          `json:"actions"`
	StatusTimestamps RunStatusTimestamps `json:"status-timestamps"`
}

// CreateRunRequest describes a run to queue against a workspace. WorkspaceID is
// required; everything else is optional and follows the server defaults when
// unset.
//
// PlanOnly is the key intent signal (a speculative plan vs. a plan+apply run).
// AutoApply and Refresh are pointers because nil means "use the workspace/server
// default" (workspace auto-apply setting; refresh defaults true) — distinct from
// an explicit false.
type CreateRunRequest struct {
	WorkspaceID            string
	ConfigurationVersionID string
	Message                string
	PlanOnly               bool
	IsDestroy              bool
	AutoApply              *bool
	TerraformVersion       string
	TargetAddrs            []string
	ReplaceAddrs           []string
	RefreshOnly            bool
	Refresh                *bool
	AllowEmptyApply        bool
	// VCSRef plans against an arbitrary branch/tag/SHA. The server forces such a
	// run plan-only regardless of PlanOnly.
	VCSRef string
}

// CreateRun queues a run. See CreateRunRequest for the intent signals. Returns
// *ValidationError (422) when the workspace/CLI policy rejects the run (e.g. a
// CLI apply on a VCS-connected workspace).
func (c *Client) CreateRun(ctx context.Context, req CreateRunRequest) (*Run, error) {
	if req.WorkspaceID == "" {
		return nil, fmt.Errorf("workspace id is required")
	}
	attrs := map[string]any{"plan-only": req.PlanOnly}
	if req.Message != "" {
		attrs["message"] = req.Message
	}
	if req.IsDestroy {
		attrs["is-destroy"] = true
	}
	if req.AutoApply != nil {
		attrs["auto-apply"] = *req.AutoApply
	}
	if req.TerraformVersion != "" {
		attrs["terraform-version"] = req.TerraformVersion
	}
	if len(req.TargetAddrs) > 0 {
		attrs["target-addrs"] = req.TargetAddrs
	}
	if len(req.ReplaceAddrs) > 0 {
		attrs["replace-addrs"] = req.ReplaceAddrs
	}
	if req.RefreshOnly {
		attrs["refresh-only"] = true
	}
	if req.Refresh != nil {
		attrs["refresh"] = *req.Refresh
	}
	if req.AllowEmptyApply {
		attrs["allow-empty-apply"] = true
	}
	if req.VCSRef != "" {
		attrs["vcs-ref"] = req.VCSRef
	}
	rels := map[string]any{
		"workspace": map[string]any{
			"data": map[string]any{"id": req.WorkspaceID, "type": "workspaces"},
		},
	}
	if req.ConfigurationVersionID != "" {
		rels["configuration-version"] = map[string]any{
			"data": map[string]any{"id": req.ConfigurationVersionID, "type": "configuration-versions"},
		}
	}
	body, err := MarshalResource("runs", attrs, rels)
	if err != nil {
		return nil, fmt.Errorf("marshal create run: %w", err)
	}
	data, err := c.Post(ctx, "/api/v2/runs", body)
	if err != nil {
		return nil, err
	}
	return parseRun(data)
}

// GetRun reads a run by id. Accepts a bare UUID or the prefixed "run-<uuid>"
// form.
func (c *Client) GetRun(ctx context.Context, runID string) (*Run, error) {
	id, err := runIDPath(runID)
	if err != nil {
		return nil, err
	}
	data, err := c.Get(ctx, "/api/v2/runs/"+id)
	if err != nil {
		return nil, err
	}
	return parseRun(data)
}

// ListWorkspaceRuns returns a workspace's runs, newest first. pageNumber and
// pageSize are 1-based; pass 0 for either to use the server defaults (page 1,
// size 20).
func (c *Client) ListWorkspaceRuns(ctx context.Context, workspaceID string, pageNumber, pageSize int) ([]Run, error) {
	if workspaceID == "" {
		return nil, fmt.Errorf("workspace id is required")
	}
	path := fmt.Sprintf("/api/v2/workspaces/%s/runs", url.PathEscape(workspaceID))
	q := url.Values{}
	if pageNumber > 0 {
		q.Set("page[number]", strconv.Itoa(pageNumber))
	}
	if pageSize > 0 {
		q.Set("page[size]", strconv.Itoa(pageSize))
	}
	if len(q) > 0 {
		path += "?" + q.Encode()
	}
	data, err := c.Get(ctx, path)
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, fmt.Errorf("parse runs list: %w", err)
	}
	out := make([]Run, 0, len(resources))
	for i := range resources {
		out = append(out, *runFromResource(&resources[i]))
	}
	return out, nil
}

// ApplyRun confirms a planned run, moving it to apply. Returns *ValidationError
// (422) or *ConflictError (409) when the run/workspace state forbids it (e.g. a
// CLI confirm on a VCS-connected workspace, or a manually-locked workspace).
func (c *Client) ApplyRun(ctx context.Context, runID string) (*Run, error) {
	return c.runAction(ctx, runID, "apply")
}

// DiscardRun discards a planned run without applying it.
func (c *Client) DiscardRun(ctx context.Context, runID string) (*Run, error) {
	return c.runAction(ctx, runID, "discard")
}

// CancelRun cancels a non-terminal run.
func (c *Client) CancelRun(ctx context.Context, runID string) (*Run, error) {
	return c.runAction(ctx, runID, "cancel")
}

// GetRunPlanJSON returns the structured JSON plan output for a run (the
// `tofu show -json` document). Plan ids share the run's UUID, so this accepts a
// run id in any form (bare, "run-<uuid>", or "plan-<uuid>"). The endpoint 302s
// to a presigned storage URL; the client follows it and returns the raw JSON
// bytes. Returns *NotFoundError when the run produced no JSON plan output (never
// planned, an engine/version that didn't emit it, or the artifact expired).
func (c *Client) GetRunPlanJSON(ctx context.Context, runID string) ([]byte, error) {
	id := strings.TrimPrefix(strings.TrimPrefix(runID, "plan-"), "run-")
	if id == "" {
		return nil, fmt.Errorf("run id is required")
	}
	// Raw JSON body (not JSON:API) — return the bytes as-is for the caller to
	// decode into whatever plan shape it needs.
	return c.Get(ctx, "/api/v2/plans/"+url.PathEscape(id)+"/json-output")
}

// ── Internal helpers ─────────────────────────────────────────────────

// runAction POSTs one of the actions/{apply,discard,cancel} endpoints (no body)
// and returns the updated run.
func (c *Client) runAction(ctx context.Context, runID, action string) (*Run, error) {
	id, err := runIDPath(runID)
	if err != nil {
		return nil, err
	}
	data, err := c.Post(ctx, "/api/v2/runs/"+id+"/actions/"+action, nil)
	if err != nil {
		return nil, err
	}
	return parseRun(data)
}

// runIDPath normalises a run id to the "run-<uuid>" form the run endpoints
// expect and path-escapes it.
func runIDPath(runID string) (string, error) {
	if runID == "" {
		return "", fmt.Errorf("run id is required")
	}
	id := runID
	if !strings.HasPrefix(id, "run-") {
		id = "run-" + strings.TrimPrefix(id, "plan-")
	}
	return url.PathEscape(id), nil
}

func parseRun(body []byte) (*Run, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse run response: %w", err)
	}
	return runFromResource(res), nil
}

func runFromResource(res *Resource) *Run {
	r := &Run{
		ID:                res.ID,
		Status:            GetStringAttr(res, "status"),
		Message:           GetStringAttr(res, "message"),
		DiscardReason:     GetStringAttr(res, "discard-reason"),
		IsDestroy:         GetBoolAttr(res, "is-destroy"),
		AutoApply:         GetBoolAttr(res, "auto-apply"),
		PlanOnly:          GetBoolAttr(res, "plan-only"),
		Source:            GetStringAttr(res, "source"),
		ExecutionBackend:  GetStringAttr(res, "execution-backend"),
		TerraformVersion:  GetStringAttr(res, "terraform-version"),
		TerragruntEnabled: GetBoolAttr(res, "terragrunt-enabled"),
		TerragruntVersion: GetStringAttr(res, "terragrunt-version"),
		ResourceCPU:       GetStringAttr(res, "resource-cpu"),
		ResourceMemory:    GetStringAttr(res, "resource-memory"),
		RunnerExitReason:  GetStringAttr(res, "runner-exit-reason"),
		RunnerExitStatus:  GetStringAttr(res, "runner-exit-status"),
		ErrorMessage:      GetStringAttr(res, "error-message"),
		TargetAddrs:       GetListAttr(res, "target-addrs"),
		ReplaceAddrs:      GetListAttr(res, "replace-addrs"),
		RefreshOnly:       GetBoolAttr(res, "refresh-only"),
		Refresh:           GetBoolAttr(res, "refresh"),
		AllowEmptyApply:   GetBoolAttr(res, "allow-empty-apply"),
		IsDriftDetection:  GetBoolAttr(res, "is-drift-detection"),
		HasJSONOutput:     GetBoolAttr(res, "has-json-output"),
		StateDiverged:     GetBoolAttr(res, "state-diverged"),
		HasCostEstimate:   GetBoolAttr(res, "has-cost-estimate"),
		CostCurrency:      GetStringAttr(res, "cost-currency"),
		WorkspaceName:     GetStringAttr(res, "workspace-name"),
		VCSCommitSHA:      GetStringAttr(res, "vcs-commit-sha"),
		VCSBranch:         GetStringAttr(res, "vcs-branch"),
		CreatedBy:         GetStringAttr(res, "created-by"),
		CreatedAt:         GetStringAttr(res, "created-at"),
		UpdatedAt:         GetStringAttr(res, "updated-at"),
	}
	if v := GetRelationshipID(res, "workspace"); v != "" {
		r.WorkspaceID = v
	}
	r.PeakMemoryBytes = nullableInt(res, "peak-memory-bytes")
	r.PeakCPUUsec = nullableInt(res, "peak-cpu-usec")
	r.RunnerExitCode = nullableInt(res, "runner-exit-code")
	r.VCSPullRequestNumber = nullableInt(res, "vcs-pull-request-number")
	r.HasChanges = nullableBool(res, "has-changes")
	r.CostMonthlyMin = nullableFloat(res, "cost-monthly-min")
	r.CostMonthlyMax = nullableFloat(res, "cost-monthly-max")
	unmarshalAttr(res, "actions", &r.Actions)
	unmarshalAttr(res, "status-timestamps", &r.StatusTimestamps)
	return r
}

// nullableInt returns a *int64 for an attribute that may be JSON null / absent.
func nullableInt(res *Resource, key string) *int64 {
	raw, ok := res.Attributes[key]
	if !ok || len(raw) == 0 || string(raw) == "null" {
		return nil
	}
	var n int64
	if json.Unmarshal(raw, &n) != nil {
		return nil
	}
	return &n
}

// nullableFloat returns a *float64 for an attribute that may be JSON null /
// absent (distinguishing "no estimate" from an explicit 0.0).
func nullableFloat(res *Resource, key string) *float64 {
	raw, ok := res.Attributes[key]
	if !ok || len(raw) == 0 || string(raw) == "null" {
		return nil
	}
	var f float64
	if json.Unmarshal(raw, &f) != nil {
		return nil
	}
	return &f
}

// nullableBool returns a *bool for an attribute that may be JSON null / absent
// (distinguishing "unknown" from an explicit false).
func nullableBool(res *Resource, key string) *bool {
	raw, ok := res.Attributes[key]
	if !ok || len(raw) == 0 || string(raw) == "null" {
		return nil
	}
	var b bool
	if json.Unmarshal(raw, &b) != nil {
		return nil
	}
	return &b
}

// unmarshalAttr decodes a nested-object attribute into dst, leaving dst at its
// zero value when the attribute is absent / null / unparsable.
func unmarshalAttr(res *Resource, key string, dst any) {
	raw, ok := res.Attributes[key]
	if !ok || len(raw) == 0 || string(raw) == "null" {
		return
	}
	_ = json.Unmarshal(raw, dst)
}
