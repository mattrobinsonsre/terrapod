package mcpserver

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"strings"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// registerObserve adds the read-rich "Observe" tools — the surface a
// state-owning platform has that a CRUD-first MCP does not: reason about and
// diagnose the estate (workspaces, runs, plan JSON, …) before acting.
func registerObserve(s *mcp.Server, c *terrapod.Client) {
	// ── terrapod_workspace_list ──────────────────────────────────────
	type workspaceListIn struct {
		PageSize int    `json:"page_size,omitempty" jsonschema:"max workspaces to return in this page (default 50)"`
		Search   string `json:"search,omitempty" jsonschema:"filter workspaces by name substring"`
	}
	type workspaceSummary struct {
		ID            string            `json:"id"`
		Name          string            `json:"name"`
		ExecutionMode string            `json:"execution_mode"`
		Locked        bool              `json:"locked"`
		DriftStatus   string            `json:"drift_status,omitempty"`
		Labels        map[string]string `json:"labels,omitempty"`
	}
	type workspaceListOut struct {
		// Returned is how many workspaces this response contains; Total is the
		// full count on the instance (post-filter). Returned < Total means the
		// result is a truncated page — narrow with `search` or raise page_size.
		Returned   int                `json:"returned"`
		Total      int                `json:"total"`
		Truncated  bool               `json:"truncated"`
		Workspaces []workspaceSummary `json:"workspaces"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_workspace_list",
		Description: "List workspaces on this Terrapod instance with their status, execution mode, lock state, drift status, and labels. Use this to orient before acting. Returns up to page_size workspaces; `total` reports the full count so you know if the result was truncated (narrow with `search`).",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in workspaceListIn) (*mcp.CallToolResult, workspaceListOut, error) {
		size := in.PageSize
		if size <= 0 {
			size = 50
		}
		list, err := c.ListWorkspaces(ctx, terrapod.WorkspaceListOptions{PageSize: size, Search: in.Search})
		if err != nil {
			return errResult(err), workspaceListOut{}, nil
		}
		// Total comes from the server's meta.pagination (see #862); it is the
		// authoritative count, not the length of this page. max() guards a
		// server that predates pagination and returns no meta (TotalCount 0).
		total := max(list.TotalCount, len(list.Items))
		out := workspaceListOut{
			Returned:  len(list.Items),
			Total:     total,
			Truncated: len(list.Items) < total,
		}
		for i := range list.Items {
			w := &list.Items[i]
			out.Workspaces = append(out.Workspaces, workspaceSummary{
				ID: w.ID, Name: w.Name, ExecutionMode: w.ExecutionMode,
				Locked: w.Locked, DriftStatus: w.DriftStatus, Labels: w.Labels,
			})
		}
		return nil, out, nil
	})

	// ── terrapod_workspace_get ───────────────────────────────────────
	type workspaceGetIn struct {
		Workspace string `json:"workspace" jsonschema:"the workspace id (ws-...) or its name"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_workspace_get",
		Description: "Get one workspace's full configuration and status by id (ws-...) or name — execution mode/backend, VCS wiring, labels, drift, lock, resource sizing.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in workspaceGetIn) (*mcp.CallToolResult, *terrapod.Workspace, error) {
		if in.Workspace == "" {
			return errText("workspace id or name is required"), nil, nil
		}
		var (
			w   *terrapod.Workspace
			err error
		)
		if strings.HasPrefix(in.Workspace, "ws-") {
			w, err = c.GetWorkspace(ctx, in.Workspace)
		} else {
			w, err = c.GetWorkspaceByName(ctx, in.Workspace)
		}
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, w, nil
	})

	// ── terrapod_run_list ────────────────────────────────────────────
	type runListIn struct {
		WorkspaceID string `json:"workspace_id" jsonschema:"the workspace id (ws-...) whose runs to list"`
		PageSize    int    `json:"page_size,omitempty" jsonschema:"max runs to return (default 20, newest first)"`
	}
	type runSummary struct {
		ID         string `json:"id"`
		Status     string `json:"status"`
		PlanOnly   bool   `json:"plan_only"`
		IsDestroy  bool   `json:"is_destroy"`
		HasChanges *bool  `json:"has_changes,omitempty"`
		Source     string `json:"source,omitempty"`
		CreatedAt  string `json:"created_at,omitempty"`
		// Which agent pool actually ran it (#1231) — on a multi-pool
		// workspace this is the only place the answer is visible.
		AgentPoolID string `json:"agent_pool_id,omitempty"`
		// Conditional auto-apply (#1274/#1301). A run held back by its mode
		// sits at `planned` looking exactly like one awaiting a human — the
		// reason is the only thing that distinguishes them, and held runs are
		// the interesting case when listing. Without these an agent has to
		// fetch every run individually to find out which need attention.
		AutoApplyMode           string `json:"auto_apply_mode,omitempty"`
		AutoApplyDeclinedReason string `json:"auto_apply_declined_reason,omitempty"`
	}
	type runListOut struct {
		Count int          `json:"count"`
		Runs  []runSummary `json:"runs"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_run_list",
		Description: "List recent runs for a workspace (newest first) with status, whether each is plan-only/destroy, whether the plan had changes, and — for conditional auto-apply — the run's mode and why it was held. A run showing auto_apply_declined_reason reached `planned` and stopped because its plan contained something its mode does not auto-apply (a destroy or replace, or an in-place update under `create`); it is waiting for a human to confirm or discard.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in runListIn) (*mcp.CallToolResult, runListOut, error) {
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), runListOut{}, nil
		}
		size := in.PageSize
		if size <= 0 {
			size = 20
		}
		runs, err := c.ListWorkspaceRuns(ctx, in.WorkspaceID, 1, size)
		if err != nil {
			return errResult(err), runListOut{}, nil
		}
		out := runListOut{Count: len(runs)}
		for i := range runs {
			r := &runs[i]
			out.Runs = append(out.Runs, runSummary{
				ID: r.ID, Status: r.Status, PlanOnly: r.PlanOnly, IsDestroy: r.IsDestroy,
				HasChanges: r.HasChanges, Source: r.Source, CreatedAt: r.CreatedAt,
				AgentPoolID:             r.AgentPoolID,
				AutoApplyMode:           r.AutoApplyMode,
				AutoApplyDeclinedReason: r.AutoApplyDeclinedReason,
			})
		}
		return nil, out, nil
	})

	// ── terrapod_run_get ─────────────────────────────────────────────
	type runGetIn struct {
		RunID string `json:"run_id" jsonschema:"the run id (run-... or a bare uuid)"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_run_get",
		Description: "Get one run's full status incl. Terrapod-native detail: has-changes, drift flag, error message, the resource profile (peak memory / runner exit), which agent pool executed it (and which pools could have), and which lifecycle actions (apply/discard/cancel) are currently permitted.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in runGetIn) (*mcp.CallToolResult, *terrapod.Run, error) {
		if in.RunID == "" {
			return errText("run_id is required"), nil, nil
		}
		r, err := c.GetRun(ctx, in.RunID)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, r, nil
	})

	// ── terrapod_run_plan_json ───────────────────────────────────────
	type planJSONIn struct {
		RunID string `json:"run_id" jsonschema:"the run id whose structured JSON plan output to fetch"`
	}
	type planJSONOut struct {
		RunID    string          `json:"run_id"`
		PlanJSON json.RawMessage `json:"plan_json"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_run_plan_json",
		Description: "Fetch the structured JSON plan output (`tofu show -json`) for a run — the resource_changes, so you can reason precisely about what a plan will create/update/destroy. Returns 'not available' if the run produced no JSON plan.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in planJSONIn) (*mcp.CallToolResult, planJSONOut, error) {
		if in.RunID == "" {
			return errText("run_id is required"), planJSONOut{}, nil
		}
		raw, err := c.GetRunPlanJSON(ctx, in.RunID)
		if err != nil {
			return errResult(err), planJSONOut{}, nil
		}
		return nil, planJSONOut{RunID: in.RunID, PlanJSON: raw}, nil
	})

	// ── terrapod_run_logs ────────────────────────────────────────────
	type runLogsIn struct {
		RunID    string `json:"run_id" jsonschema:"the run id (run-...) whose log to fetch"`
		Phase    string `json:"phase,omitempty" jsonschema:"which phase to read: plan (default) or apply"`
		Offset   int64  `json:"offset,omitempty" jsonschema:"byte offset to read from; omit to get the END of the log, which is where a failure is"`
		MaxBytes int64  `json:"max_bytes,omitempty" jsonschema:"cap on returned bytes (default 16384)"`
	}
	type runLogsOut struct {
		RunID     string `json:"run_id"`
		Phase     string `json:"phase"`
		Log       string `json:"log"`
		Offset    int64  `json:"offset"`
		Bytes     int64  `json:"bytes"`
		Truncated bool   `json:"truncated"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_run_logs",
		Description: "Fetch a run's plan or apply LOG — the terraform/tofu output, which is where the reason for a failure actually is. terrapod_run_get reports THAT a run errored and its exit code; this reports WHY. " +
			"Returns the END of the log by default, because that is where an error is reported and a full apply log can be megabytes; `truncated` says whether earlier output was dropped and `offset` says where the returned chunk starts, so pass that offset back to page further in. ANSI colour codes are stripped. " +
			"An empty log is not an error: a run that has not reached the phase yet simply has nothing to show.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in runLogsIn) (*mcp.CallToolResult, runLogsOut, error) {
		if in.RunID == "" {
			return errText("run_id is required"), runLogsOut{}, nil
		}
		phase := in.Phase
		if phase == "" {
			phase = "plan"
		}
		if phase != "plan" && phase != "apply" {
			return errText("phase must be 'plan' or 'apply'"), runLogsOut{}, nil
		}

		opts := &terrapod.LogOptions{Plain: true, Offset: in.Offset}
		get := c.GetPlanLog
		if phase == "apply" {
			get = c.GetApplyLog
		}
		raw, err := get(ctx, in.RunID, opts)
		if err != nil {
			return errResult(err), runLogsOut{}, nil
		}

		max := in.MaxBytes
		if max <= 0 {
			max = 16384
		}
		out := runLogsOut{RunID: in.RunID, Phase: phase, Offset: in.Offset}
		if int64(len(raw)) > max {
			out.Truncated = true
			if in.Offset > 0 {
				// An explicit offset means the caller is paging forward, so
				// keep the FRONT of their window; tailing it would silently
				// skip the very bytes they asked for.
				raw = raw[:max]
			} else {
				// No offset: keep the END. A failure is reported last, so the
				// tail is the part worth spending context on.
				start := int64(len(raw)) - max
				// Advance to a line boundary. A byte-sized tail lands mid-line,
				// and the severed fragment reads as corrupt output rather than
				// as a truncation — noise a reader has to reason past. Only
				// when a newline is actually in range.
				if nl := bytes.IndexByte(raw[start:], '\n'); nl >= 0 && nl < len(raw[start:])-1 {
					start += int64(nl) + 1
				}
				out.Offset = start
				raw = raw[start:]
			}
		}
		out.Log = string(raw)
		out.Bytes = int64(len(raw))
		return nil, out, nil
	})

	// ── terrapod_workspace_cost ──────────────────────────────────────
	type wsCostIn struct {
		WorkspaceID string `json:"workspace_id" jsonschema:"the workspace id (ws-...) whose current managed-infrastructure monthly cost to estimate"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_workspace_cost",
		Description: "Estimate the CURRENT monthly cost of a workspace's managed infrastructure from its latest state (native cost engine — data only, no AI). Returns currency, the total monthly range, per-resource costs, the unpriced resources, and which state version was priced. `state-version` is null when the workspace has no state yet.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in wsCostIn) (*mcp.CallToolResult, *terrapod.WorkspaceCostEstimate, error) {
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), nil, nil
		}
		e, err := c.GetWorkspaceCostEstimate(ctx, in.WorkspaceID)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, e, nil
	})

	// ── terrapod_ha_status ───────────────────────────────────────────
	type haStatusIn struct{}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_ha_status",
		Description: "Report this Terrapod deployment's HA posture — both halves of it. (1) THE PAIR, the question to answer BEFORE a failover: on a FOLLOWER read `in-sync`, `seconds-since-last-sync` and `backfilling-classes` — a node mid-backfill is NOT in sync however recent its last sync, so treat a non-empty `backfilling-classes` as not-ready regardless of the timestamp; on a LEADER compare `oldest-event-age-seconds` against `retention-seconds`, since as they converge the follower is close to falling off the retained event window. `peer-configured` false means an ordinary single node and none of the pair fields apply. There is deliberately no 'N events behind' — seconds since the last SUCCESSFUL pull is the honest number, because a pull that returned nothing means caught up as of then. (2) IN-CLUSTER REDUNDANCY, which applies even with no peer: `components` gives ready-vs-desired per component and `single-replica-components` names those on exactly one ready replica — a deployment serving from one API pod is not highly available however well it replicates. If `components-unavailable-reason` is set the API could not read its namespace (usually a declined RBAC Role): that is UNKNOWN, not an outage, so never report an empty `components` as components being down. `ha-findings` lists SPECIFIC gaps and is the actionable part: `no-pdb` (a node drain can evict every replica at once), `pdb-blocks-eviction` (a PodDisruptionBudget that permits NO voluntary eviction — looks protective, actually stalls node drains), `node-concentration` and `zone-concentration`. Findings are raised ONLY where the cluster could have done better: a single-node k3s/kind cluster and a single-AZ deployment produce none, because neither could have spread. An EMPTY `ha-findings` therefore means 'nothing avoidable was found', not 'not checked'. `schedulable-nodes`/`cluster-zones` null means node reads were declined (opt-in, cluster-scoped) — placement is still reported but never judged. Even with everything visible this reports specific findings, never an overall verdict: PodDisruptionBudget coverage is checked, but anti-affinity and topology-spread configuration are not observable from a pod list, so do not claim HA is correctly configured.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, _ haStatusIn) (*mcp.CallToolResult, *terrapod.HAStatus, error) {
		st, err := c.GetHAStatus(ctx)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, st, nil
	})

	// ── terrapod_workspace_architecture_critique ─────────────────────
	type wsCritiqueIn struct {
		WorkspaceID string `json:"workspace_id" jsonschema:"the workspace id (ws-...) whose current-state architecture critique to fetch"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_workspace_architecture_critique",
		Description: "Get the AI architecture critique of a workspace's CURRENT deployed system, inferred from its latest Terraform state (the optional ai_architecture feature). Unlike a run's plan summary — which reviews a change — this reviews the system as it EXISTS, across reliability/security/cost/operations/scalability. Every finding is grounded (security ← the Checkov/Trivy scanner, carrying the rule id in `grounded_in`; cost ← the cost engine; reliability/operations ← state + resource graph) and anchored to a resource address; concerns the model couldn't judge from the data are listed under `deferred` rather than guessed. Returns the inferred `architecture` (summary, tiers, data stores, blast radius), an overall `risk-level`, and ranked `findings`. Returns 'not available' when the feature is disabled, the workspace has no state, or no critique exists for the current state yet. Branch on `status`: ready | pending | skipped | errored.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in wsCritiqueIn) (*mcp.CallToolResult, *terrapod.ArchitectureCritique, error) {
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), nil, nil
		}
		cr, err := c.GetArchitectureCritique(ctx, in.WorkspaceID)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, cr, nil
	})

	// ── terrapod_run_cost ────────────────────────────────────────────
	type runCostIn struct {
		RunID string `json:"run_id" jsonschema:"the run id (run-... or a bare uuid) whose plan cost delta to fetch"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_run_cost",
		Description: "Get a run's monthly cost estimate — the plan's cost DELTA: the projected monthly total, the delta this run introduces, the previous total, per-resource costs, and unpriced resources (native cost engine — data only, no AI). Returns 'not available' when the run produced no cost estimate.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in runCostIn) (*mcp.CallToolResult, *terrapod.CostEstimate, error) {
		if in.RunID == "" {
			return errText("run_id is required"), nil, nil
		}
		e, err := c.GetRunCostEstimate(ctx, in.RunID)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, e, nil
	})

	// ── terrapod_run_security_scan ───────────────────────────────────
	type runScanIn struct {
		RunID string `json:"run_id" jsonschema:"the run id (run-... or a bare uuid) whose IaC security-scan result to fetch"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_run_security_scan",
		Description: "Get a run's deterministic IaC security-scan result (Checkov/Trivy misconfiguration scan). Returns the engine, enforcement level (off/advisory/enforced), severity threshold, outcome (passed/failed/errored), the normalised findings (rule id, severity, resource, file:line), a summary (total + blocking counts), and any override. Returns null when the workspace has scanning off or the run wasn't scanned.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in runScanIn) (*mcp.CallToolResult, *terrapod.SecurityScan, error) {
		if in.RunID == "" {
			return errText("run_id is required"), nil, nil
		}
		sc, err := c.GetRunSecurityScan(ctx, in.RunID)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, sc, nil
	})

	// ── terrapod_deleted_workspace_list ──────────────────────────────
	type deletedWSIn struct{}
	type deletedWSOut struct {
		Count   int                         `json:"count"`
		Deleted []terrapod.DeletedWorkspace `json:"deleted_workspaces"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_deleted_workspace_list",
		Description: "List deleted workspaces whose state is still recoverable. Requires platform admin. " +
			"Deleting a workspace removes its rows but not its state blobs; a delete marker keeps them findable " +
			"until the retention window expires, after which they are reaped and the workspace is gone for good — " +
			"so `restorable-until` is the field to act on, and an empty value means retention is disabled and " +
			"nothing is reaped automatically. `marker-reason` distinguishes `deleted` (written by the delete, so " +
			"`deleted-at` is the real deletion time) from `discovered-orphaned` (the reaper found state with no " +
			"marker, so `deleted-at` is merely when it was first seen — treat that date as a floor, not a fact). " +
			"`state-versions-available` is counted from storage at request time, so a partial reap shows up there " +
			"rather than in the marker. `variable-names` carries names and categories ONLY — values are never " +
			"recorded — so use it to tell the user what they will have to recreate after a restore.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, _ deletedWSIn) (*mcp.CallToolResult, *deletedWSOut, error) {
		items, err := c.ListAllDeletedWorkspaces(ctx)
		if err != nil {
			return errResult(err), nil, nil
		}
		// A nil map/slice marshals to `null`, which fails the tool's derived
		// output schema ("want object") and takes the whole call down rather
		// than degrading. The server always sends `{}`/`[]` today, but a
		// marker written by another version need not, and one absent field
		// should not cost the agent the entire listing.
		for i := range items {
			if items[i].Settings == nil {
				items[i].Settings = map[string]any{}
			}
			if items[i].VariableNames == nil {
				items[i].VariableNames = []terrapod.DeletedWorkspaceVariable{}
			}
		}
		return nil, &deletedWSOut{Count: len(items), Deleted: items}, nil
	})
}

// errResult turns a go-terrapod error into an agent-facing tool error,
// distinguishing an expired token (actionable: re-login) from an RBAC denial
// (the user's Terrapod role lacks the capability — re-login won't help).
func errResult(err error) *mcp.CallToolResult {
	var (
		authnErr *terrapod.AuthenticationError
		authzErr *terrapod.AuthorizationError
	)
	switch {
	case errors.As(err, &authnErr):
		return errText("authentication failed — the Terrapod token is missing or expired. " +
			"Ask the user to run `tofu login <host>` and retry. (" + err.Error() + ")")
	case errors.As(err, &authzErr):
		return errText("permission denied — the user's Terrapod role lacks the capability for this action " +
			"(re-login won't help; an admin must grant it). (" + err.Error() + ")")
	default:
		return errText(err.Error())
	}
}

// errText builds a tool result flagged as an error carrying a message the agent
// relays to the user.
func errText(msg string) *mcp.CallToolResult {
	return &mcp.CallToolResult{
		IsError: true,
		Content: []mcp.Content{&mcp.TextContent{Text: msg}},
	}
}
