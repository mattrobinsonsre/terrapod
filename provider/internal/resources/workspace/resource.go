package workspace

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/boolplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/setplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var (
	_ resource.Resource                = &workspaceResource{}
	_ resource.ResourceWithImportState = &workspaceResource{}
)

// workspaceResource holds two clients during the provider's migration to
// go-terrapod (#347). Workspace CRUD uses the new typed methods on `tc`;
// the remote-state-consumers helpers below still use `client` because
// they live outside the workspaces resource and migrate in a later pass
// alongside the standalone terrapod_remote_state_consumer resource. Once
// both have migrated, the `client` field disappears.
type workspaceResource struct {
	client *client.Client
	tc     *terrapod.Client
}

// NewResource returns a new workspace resource.
func NewResource() resource.Resource {
	return &workspaceResource{}
}

func (r *workspaceResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_workspace"
}

func (r *workspaceResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a Terrapod workspace.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description: "The workspace ID (e.g. ws-abc123).",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"name": schema.StringAttribute{
				Description: "The workspace name.",
				Required:    true,
			},
			"execution_mode": schema.StringAttribute{
				Description: "Execution mode: local or agent.",
				Optional:    true,
				Computed:    true,
				Default:     stringdefault.StaticString("local"),
			},
			"auto_apply": schema.BoolAttribute{
				Description: "Automatically apply successful plans.",
				Optional:    true,
				Computed:    true,
				Default:     booldefault.StaticBool(false),
			},
			"execution_backend": schema.StringAttribute{
				Description: "Execution backend: terraform or tofu.",
				Optional:    true,
				Computed:    true,
				Default:     stringdefault.StaticString("terraform"),
			},
			"terraform_version": schema.StringAttribute{
				Description: "The Terraform/OpenTofu version to use.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"working_directory": schema.StringAttribute{
				Description: "Working directory relative to the repo root.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"resource_cpu": schema.StringAttribute{
				Description: "CPU request for runner Jobs (K8s format, e.g. '1').",
				Optional:    true,
				Computed:    true,
				Default:     stringdefault.StaticString("1"),
			},
			"resource_memory": schema.StringAttribute{
				Description: "Memory request for runner Jobs (K8s format, e.g. '2Gi').",
				Optional:    true,
				Computed:    true,
				Default:     stringdefault.StaticString("2Gi"),
			},
			"labels": schema.MapAttribute{
				Description: "Labels for RBAC evaluation (key-value pairs).",
				Optional:    true,
				ElementType: types.StringType,
			},
			"vcs_repo_url": schema.StringAttribute{
				Description: "Git HTTPS URL for VCS integration.",
				Optional:    true,
			},
			"vcs_branch": schema.StringAttribute{
				Description: "Branch to track (empty = repo default).",
				Optional:    true,
			},
			"vcs_connection_id": schema.StringAttribute{
				Description: "VCS connection ID (e.g. vcs-abc123).",
				Optional:    true,
			},
			"vcs_workflow": schema.StringAttribute{
				Description: "VCS workflow mode: `merge_then_apply` (default, TFE/HCP standard) or `apply_then_merge` (Atlantis-style; PR runs are full plan-and-apply that wait on a `terrapod apply` comment). Apply-then-merge requires a VCS connection and is incompatible with `auto_apply=true`.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"auto_merge": schema.BoolAttribute{
				Description: "If true, Terrapod merges the PR/MR after a successful apply (subject to branch protection). Default: false.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.Bool{
					boolplanmodifier.UseStateForUnknown(),
				},
			},
			"auto_merge_strategy": schema.StringAttribute{
				Description: "Merge strategy for auto-merge: `merge` (default), `squash`, or `rebase`.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"agent_pool_id": schema.StringAttribute{
				Description: "Agent pool ID for agent execution mode.",
				Optional:    true,
			},
			"var_files": schema.ListAttribute{
				Description: "List of .tfvars file paths passed as -var-file arguments to plan/apply.",
				Optional:    true,
				ElementType: types.StringType,
			},
			"trigger_prefixes": schema.ListAttribute{
				Description: "Repo-root-relative directories to include in the sparse VCS fetch in addition to `working_directory`. Required when the workspace's terraform crosses directory boundaries via relative module sources (`module \"foo\" { source = \"../foo\" }`) — sparse-checkout cone mode includes parents of the listed directories but NOT siblings, so the referenced sibling must be declared here or the runner will error with `Unable to evaluate directory symlink`.",
				Optional:    true,
				ElementType: types.StringType,
			},
			"drift_detection_enabled": schema.BoolAttribute{
				Description: "Enable drift detection for this workspace. Defaults to true for VCS-connected workspaces, false otherwise.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.Bool{
					boolplanmodifier.UseStateForUnknown(),
				},
			},
			"drift_detection_interval_seconds": schema.Int64Attribute{
				Description: "Interval in seconds between drift detection checks.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.Int64{
					int64planmodifier.UseStateForUnknown(),
				},
			},
			"ai_summary_mode": schema.StringAttribute{
				Description: "Per-workspace AI plan-summary opt-in (#401). One of \"default\" (follow the deployment's global `ai_summary.enabled` setting), \"enabled\" (always summarise this workspace's plans), or \"disabled\" (never summarise — overrides global). Defaults to \"default\".",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"ai_summary_context": schema.StringAttribute{
				Description: "Workspace-specific facts appended to the AI summariser's prompt (#401). Additive to the deployment-wide fleet context. Use to flag blast-radius concerns or domain knowledge the model should weigh when describing changes for this workspace. Max 4000 characters.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"remote_state_consumers": schema.SetAttribute{
				Description: "Workspace IDs authorized to read this workspace's state via `terraform_remote_state` (#344). Optional + Computed: leave null to opt out of managing the set here (server side is left intact — useful when consumers are managed via standalone `terrapod_remote_state_consumer` resources elsewhere). Set to `[]` to explicitly remove all consumers. **Do not mix this attribute with standalone `terrapod_remote_state_consumer` resources targeting the same producer** — the two will drift on every plan and fight each other. Mutations require admin/write on this (producer) workspace; a consumer team cannot self-grant.",
				Optional:    true,
				Computed:    true,
				ElementType: types.StringType,
				PlanModifiers: []planmodifier.Set{
					setplanmodifier.UseStateForUnknown(),
				},
			},

			// Read-only
			"owner_email": schema.StringAttribute{
				Description: "Email of the workspace owner.",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			// `UseStateForUnknown` ONLY belongs on Computed-only attrs
			// whose value definitely does NOT change as a side effect of
			// the apply. v0.35.5 applied it everywhere and broke apply
			// on every workspace because the server-volatile timestamps
			// (updated_at ticks on every PATCH; vcs_last_polled_at ticks
			// on every poll cycle) produced plan-vs-apply-time mismatches
			// → terraform-plugin-framework's consistency check aborted.
			//
			// Safe-for-UseStateForUnknown set (each field's invariant):
			//   created_at         — immutable after creation
			//   owner_email        — only the platform admin can change; PATCH never does
			//   agent_pool_name    — only changes when agent_pool_id changes (caller's PATCH)
			//   vcs_connection_name — only changes when vcs_connection_id changes
			//   state_diverged     — only set/cleared by a state upload pathway; not by PATCH
			//
			// Server-volatile set (NO UseStateForUnknown — plan will
			// honestly show `(known after apply)`; the diff noise is the
			// right answer because the value can legitimately change
			// between plan and apply):
			//   updated_at, vcs_last_polled_at, vcs_last_error,
			//   vcs_last_error_at, drift_status, drift_last_checked_at,
			//   drift_latest_run_id, lifecycle_state, lifecycle_reason,
			//   locked
			"drift_status": schema.StringAttribute{
				Description: "Current drift status: \"\" (never checked), \"no_drift\", \"drifted\", or \"errored\". Server-volatile — updates when a drift run completes.",
				Computed:    true,
			},
			"drift_last_checked_at": schema.StringAttribute{
				Description: "Timestamp of the last drift check. Server-volatile.",
				Computed:    true,
			},
			"drift_latest_run_id": schema.StringAttribute{
				Description: "ID of the drift run that produced the current `drift_status`, prefixed `run-…`. Empty when drift has never run or when cleared by a successful apply. Server-volatile.",
				Computed:    true,
			},
			"state_diverged": schema.BoolAttribute{
				Description: "True when an apply Job succeeded but uploading the resulting state to Terrapod failed; the recorded state is out of sync with reality. Stable across PATCHes — only the state-upload pathway changes this.",
				Computed:    true,
				PlanModifiers: []planmodifier.Bool{
					boolplanmodifier.UseStateForUnknown(),
				},
			},
			"lifecycle_state": schema.StringAttribute{
				Description: "Autodiscovery lifecycle state for managed workspaces: \"active\", \"pending_deletion\", or \"archived\". Server-volatile — autodiscovery lifecycle reconciler can move this between plan and apply.",
				Computed:    true,
			},
			"lifecycle_reason": schema.StringAttribute{
				Description: "Human-readable explanation of `lifecycle_state`. Empty for active workspaces. Server-volatile.",
				Computed:    true,
			},
			"vcs_last_polled_at": schema.StringAttribute{
				Description: "Timestamp of the most recent successful VCS poll cycle. Server-volatile — VCS poller writes this every `vcs.poll_interval_seconds` (default 60s).",
				Computed:    true,
			},
			"vcs_last_error": schema.StringAttribute{
				Description: "Most recent VCS poll error message. Empty when the last poll succeeded. Server-volatile.",
				Computed:    true,
			},
			"vcs_last_error_at": schema.StringAttribute{
				Description: "Timestamp of `vcs_last_error`. Server-volatile.",
				Computed:    true,
			},
			"agent_pool_name": schema.StringAttribute{
				Description: "Human-readable name of the assigned agent pool, server-derived from `agent_pool_id`. Only changes when `agent_pool_id` changes.",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"vcs_connection_name": schema.StringAttribute{
				Description: "Human-readable name of the assigned VCS connection, server-derived from `vcs_connection_id`. Only changes when `vcs_connection_id` changes.",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"locked": schema.BoolAttribute{
				Description: "Whether the workspace is locked. Server-volatile — operators can lock/unlock via the API outside of Terraform.",
				Computed:    true,
			},
			"created_at": schema.StringAttribute{
				Description: "Creation timestamp.",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"updated_at": schema.StringAttribute{
				Description: "Last update timestamp. Server-volatile — ticks on every PATCH and on every server-side write (drift detection, VCS poll, lifecycle reconciler).",
				Computed:    true,
			},
		},
	}
}

func (r *workspaceResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	c, ok := req.ProviderData.(*client.Client)
	if !ok {
		resp.Diagnostics.AddError("Unexpected provider data type", fmt.Sprintf("Expected *client.Client, got %T", req.ProviderData))
		return
	}
	r.client = c

	// Build the go-terrapod client from the same BaseURL+Token. Both
	// clients share the operator's auth + endpoint configuration; only
	// the call shapes differ. SkipTLSVerify is captured indirectly via
	// the shared HTTPClient when the provider was configured with it
	// — we re-derive here so go-terrapod's defaults (TLS 1.3 minimum)
	// apply consistently with the rest of the SDK consumers.
	tc, err := terrapod.NewClient(terrapod.Options{
		BaseURL: c.BaseURL,
		Token:   c.Token,
	})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	r.tc = tc
}

func (r *workspaceResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan workspaceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	createReq, dgs := buildCreateWorkspaceRequest(ctx, &plan)
	resp.Diagnostics.Append(dgs...)
	if resp.Diagnostics.HasError() {
		return
	}

	ws, err := r.tc.CreateWorkspace(ctx, createReq)
	if err != nil {
		resp.Diagnostics.AddError("Failed to create workspace", err.Error())
		return
	}

	resp.Diagnostics.Append(readWorkspaceIntoModel(ctx, ws, &plan)...)

	// Apply the consumer set from plan (if managed here), then refresh
	// the attribute from the server (#344, #348). Null in plan ⇒
	// unmanaged ⇒ no PUT but we still read for state consistency.
	if err := applyConsumersFromPlan(ctx, r.tc, plan.ID.ValueString(), plan.RemoteStateConsumers); err != nil {
		resp.Diagnostics.AddError("Failed to apply remote_state_consumers", err.Error())
		return
	}
	consumers, dgs := readRemoteStateConsumers(ctx, r.tc, plan.ID.ValueString())
	resp.Diagnostics.Append(dgs...)
	plan.RemoteStateConsumers = consumers

	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *workspaceResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state workspaceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	ws, err := r.tc.GetWorkspace(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Failed to read workspace", err.Error())
		return
	}

	resp.Diagnostics.Append(readWorkspaceIntoModel(ctx, ws, &state)...)

	// Refresh the consumer set from server (#344, #348). Always read,
	// even when the user manages this via standalone resources — the
	// Optional+Computed schema means a null config value falls back
	// to the state value during plan, so no spurious diff.
	consumers, dgs := readRemoteStateConsumers(ctx, r.tc, state.ID.ValueString())
	resp.Diagnostics.Append(dgs...)
	state.RemoteStateConsumers = consumers

	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *workspaceResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan workspaceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var state workspaceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	updateReq, dgs := buildUpdateWorkspaceRequest(ctx, &plan)
	resp.Diagnostics.Append(dgs...)
	if resp.Diagnostics.HasError() {
		return
	}

	ws, err := r.tc.UpdateWorkspace(ctx, state.ID.ValueString(), updateReq)
	if err != nil {
		resp.Diagnostics.AddError("Failed to update workspace", err.Error())
		return
	}

	resp.Diagnostics.Append(readWorkspaceIntoModel(ctx, ws, &plan)...)

	// Apply the consumer set from plan if managed here, then refresh.
	// Same null = unmanaged convention as Create (#344, #348).
	if err := applyConsumersFromPlan(ctx, r.tc, plan.ID.ValueString(), plan.RemoteStateConsumers); err != nil {
		resp.Diagnostics.AddError("Failed to apply remote_state_consumers", err.Error())
		return
	}
	consumers, dgs := readRemoteStateConsumers(ctx, r.tc, plan.ID.ValueString())
	resp.Diagnostics.Append(dgs...)
	plan.RemoteStateConsumers = consumers

	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *workspaceResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state workspaceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	// go-terrapod handles the Terrapod-native path (/api/terrapod/v1/
	// rather than /api/v2/, which returns 405 — see #353). The 404-on-
	// idempotent-delete is unwrapped to match the original behaviour.
	err := r.tc.DeleteWorkspace(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Failed to delete workspace", err.Error())
		}
	}
}

func (r *workspaceResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	// Import by workspace name — resolve to ID via go-terrapod's
	// GetWorkspaceByName. Terraform then calls Read with the resolved
	// id to populate the rest of the state.
	ws, err := r.tc.GetWorkspaceByName(ctx, req.ID)
	if err != nil {
		resp.Diagnostics.AddError("Failed to import workspace", fmt.Sprintf("Could not find workspace %q: %s", req.ID, err))
		return
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), ws.ID)...)
}

// buildCreateWorkspaceRequest translates a Terraform plan into the
// go-terrapod CreateWorkspaceRequest. Optional fields the operator
// didn't set (IsNull / IsUnknown) stay zero-valued in the struct so
// the SDK omits them from the JSON:API attributes — matching the
// previous map-based behaviour where only set keys were sent.
func buildCreateWorkspaceRequest(ctx context.Context, m *workspaceModel) (terrapod.CreateWorkspaceRequest, diag.Diagnostics) {
	var diags diag.Diagnostics
	req := terrapod.CreateWorkspaceRequest{Name: m.Name.ValueString()}

	if !m.ExecutionMode.IsNull() && !m.ExecutionMode.IsUnknown() {
		req.ExecutionMode = m.ExecutionMode.ValueString()
	}
	if !m.AutoApply.IsNull() && !m.AutoApply.IsUnknown() {
		v := m.AutoApply.ValueBool()
		req.AutoApply = &v
	}
	if !m.ExecutionBackend.IsNull() && !m.ExecutionBackend.IsUnknown() {
		req.ExecutionBackend = m.ExecutionBackend.ValueString()
	}
	if !m.TerraformVersion.IsNull() && !m.TerraformVersion.IsUnknown() {
		req.TerraformVersion = m.TerraformVersion.ValueString()
	}
	if !m.WorkingDirectory.IsNull() && !m.WorkingDirectory.IsUnknown() {
		req.WorkingDirectory = m.WorkingDirectory.ValueString()
	}
	if !m.ResourceCPU.IsNull() && !m.ResourceCPU.IsUnknown() {
		req.ResourceCPU = m.ResourceCPU.ValueString()
	}
	if !m.ResourceMemory.IsNull() && !m.ResourceMemory.IsUnknown() {
		req.ResourceMemory = m.ResourceMemory.ValueString()
	}
	if !m.VCSRepoURL.IsNull() {
		req.VCSRepoURL = m.VCSRepoURL.ValueString()
	}
	if !m.VCSBranch.IsNull() {
		req.VCSBranch = m.VCSBranch.ValueString()
	}
	if !m.VCSWorkflow.IsNull() && !m.VCSWorkflow.IsUnknown() {
		req.VCSWorkflow = m.VCSWorkflow.ValueString()
	}
	if !m.AutoMerge.IsNull() && !m.AutoMerge.IsUnknown() {
		v := m.AutoMerge.ValueBool()
		req.AutoMerge = &v
	}
	if !m.AutoMergeStrategy.IsNull() && !m.AutoMergeStrategy.IsUnknown() {
		req.AutoMergeStrategy = m.AutoMergeStrategy.ValueString()
	}
	if !m.VCSConnectionID.IsNull() && !m.VCSConnectionID.IsUnknown() {
		req.VCSConnectionID = m.VCSConnectionID.ValueString()
	}
	if !m.AgentPoolID.IsNull() {
		req.AgentPoolID = m.AgentPoolID.ValueString()
	}
	if !m.Labels.IsNull() && !m.Labels.IsUnknown() {
		labels := map[string]string{}
		for k, v := range m.Labels.Elements() {
			labels[k] = v.(types.String).ValueString()
		}
		req.Labels = labels
	}
	if !m.VarFiles.IsNull() && !m.VarFiles.IsUnknown() {
		varFiles := []string{}
		for _, v := range m.VarFiles.Elements() {
			varFiles = append(varFiles, v.(types.String).ValueString())
		}
		req.VarFiles = varFiles
	}
	if !m.TriggerPrefixes.IsNull() && !m.TriggerPrefixes.IsUnknown() {
		triggerPrefixes := []string{}
		for _, v := range m.TriggerPrefixes.Elements() {
			triggerPrefixes = append(triggerPrefixes, v.(types.String).ValueString())
		}
		req.TriggerPrefixes = triggerPrefixes
	}
	if !m.DriftDetectionEnabled.IsNull() && !m.DriftDetectionEnabled.IsUnknown() {
		v := m.DriftDetectionEnabled.ValueBool()
		req.DriftDetectionEnabled = &v
	}
	if !m.DriftDetectionIntervalSeconds.IsNull() && !m.DriftDetectionIntervalSeconds.IsUnknown() {
		v := m.DriftDetectionIntervalSeconds.ValueInt64()
		req.DriftDetectionIntervalSeconds = &v
	}
	if !m.AISummaryMode.IsNull() && !m.AISummaryMode.IsUnknown() {
		req.AISummaryMode = m.AISummaryMode.ValueString()
	}
	if !m.AISummaryContext.IsNull() && !m.AISummaryContext.IsUnknown() {
		req.AISummaryContext = m.AISummaryContext.ValueString()
	}
	return req, diags
}

// buildUpdateWorkspaceRequest is the partial-update counterpart to
// buildCreateWorkspaceRequest. Same translation logic; Name is
// included so a Terraform-driven rename round-trips via PATCH (the
// API supports rename — terrapod-vcs-test moves between names
// during e.g. the cutover smoke).
func buildUpdateWorkspaceRequest(ctx context.Context, m *workspaceModel) (terrapod.UpdateWorkspaceRequest, diag.Diagnostics) {
	var diags diag.Diagnostics
	req := terrapod.UpdateWorkspaceRequest{}

	if !m.Name.IsNull() && !m.Name.IsUnknown() {
		req.Name = m.Name.ValueString()
	}
	if !m.ExecutionMode.IsNull() && !m.ExecutionMode.IsUnknown() {
		req.ExecutionMode = m.ExecutionMode.ValueString()
	}
	if !m.AutoApply.IsNull() && !m.AutoApply.IsUnknown() {
		v := m.AutoApply.ValueBool()
		req.AutoApply = &v
	}
	if !m.ExecutionBackend.IsNull() && !m.ExecutionBackend.IsUnknown() {
		req.ExecutionBackend = m.ExecutionBackend.ValueString()
	}
	if !m.TerraformVersion.IsNull() && !m.TerraformVersion.IsUnknown() {
		req.TerraformVersion = m.TerraformVersion.ValueString()
	}
	if !m.WorkingDirectory.IsNull() && !m.WorkingDirectory.IsUnknown() {
		req.WorkingDirectory = m.WorkingDirectory.ValueString()
	}
	if !m.ResourceCPU.IsNull() && !m.ResourceCPU.IsUnknown() {
		req.ResourceCPU = m.ResourceCPU.ValueString()
	}
	if !m.ResourceMemory.IsNull() && !m.ResourceMemory.IsUnknown() {
		req.ResourceMemory = m.ResourceMemory.ValueString()
	}
	if !m.VCSRepoURL.IsNull() {
		req.VCSRepoURL = m.VCSRepoURL.ValueString()
	}
	if !m.VCSBranch.IsNull() {
		req.VCSBranch = m.VCSBranch.ValueString()
	}
	if !m.VCSWorkflow.IsNull() && !m.VCSWorkflow.IsUnknown() {
		req.VCSWorkflow = m.VCSWorkflow.ValueString()
	}
	if !m.AutoMerge.IsNull() && !m.AutoMerge.IsUnknown() {
		v := m.AutoMerge.ValueBool()
		req.AutoMerge = &v
	}
	if !m.AutoMergeStrategy.IsNull() && !m.AutoMergeStrategy.IsUnknown() {
		req.AutoMergeStrategy = m.AutoMergeStrategy.ValueString()
	}
	if !m.VCSConnectionID.IsNull() && !m.VCSConnectionID.IsUnknown() {
		req.VCSConnectionID = m.VCSConnectionID.ValueString()
	}
	if !m.AgentPoolID.IsNull() {
		req.AgentPoolID = m.AgentPoolID.ValueString()
	}
	if !m.Labels.IsNull() && !m.Labels.IsUnknown() {
		labels := map[string]string{}
		for k, v := range m.Labels.Elements() {
			labels[k] = v.(types.String).ValueString()
		}
		req.Labels = labels
	}
	if !m.VarFiles.IsNull() && !m.VarFiles.IsUnknown() {
		varFiles := []string{}
		for _, v := range m.VarFiles.Elements() {
			varFiles = append(varFiles, v.(types.String).ValueString())
		}
		req.VarFiles = varFiles
	}
	if !m.TriggerPrefixes.IsNull() && !m.TriggerPrefixes.IsUnknown() {
		triggerPrefixes := []string{}
		for _, v := range m.TriggerPrefixes.Elements() {
			triggerPrefixes = append(triggerPrefixes, v.(types.String).ValueString())
		}
		req.TriggerPrefixes = triggerPrefixes
	}
	if !m.DriftDetectionEnabled.IsNull() && !m.DriftDetectionEnabled.IsUnknown() {
		v := m.DriftDetectionEnabled.ValueBool()
		req.DriftDetectionEnabled = &v
	}
	if !m.DriftDetectionIntervalSeconds.IsNull() && !m.DriftDetectionIntervalSeconds.IsUnknown() {
		v := m.DriftDetectionIntervalSeconds.ValueInt64()
		req.DriftDetectionIntervalSeconds = &v
	}
	if !m.AISummaryMode.IsNull() && !m.AISummaryMode.IsUnknown() {
		req.AISummaryMode = m.AISummaryMode.ValueString()
	}
	if !m.AISummaryContext.IsNull() && !m.AISummaryContext.IsUnknown() {
		v := m.AISummaryContext.ValueString()
		req.AISummaryContext = &v
	}
	return req, diags
}

// readWorkspaceIntoModel populates the Terraform model from a typed
// go-terrapod Workspace. Replaces the previous Resource/Attribute-map
// based readResourceIntoModel — the SDK does the JSON:API parsing now.
func readWorkspaceIntoModel(ctx context.Context, ws *terrapod.Workspace, m *workspaceModel) diag.Diagnostics {
	var diags diag.Diagnostics

	m.ID = types.StringValue(ws.ID)
	m.Name = types.StringValue(ws.Name)
	m.ExecutionMode = types.StringValue(ws.ExecutionMode)
	m.AutoApply = types.BoolValue(ws.AutoApply)
	m.ExecutionBackend = types.StringValue(ws.ExecutionBackend)
	m.WorkingDirectory = types.StringValue(ws.WorkingDirectory)
	m.ResourceCPU = types.StringValue(ws.ResourceCPU)
	m.ResourceMemory = types.StringValue(ws.ResourceMemory)
	m.VCSWorkflow = types.StringValue(ws.VCSWorkflow)
	m.AutoMerge = types.BoolValue(ws.AutoMerge)
	m.AutoMergeStrategy = types.StringValue(ws.AutoMergeStrategy)
	m.OwnerEmail = types.StringValue(ws.OwnerEmail)
	m.Locked = types.BoolValue(ws.Locked)
	m.CreatedAt = types.StringValue(ws.CreatedAt)
	m.UpdatedAt = types.StringValue(ws.UpdatedAt)

	// Nullable string fields — empty string from the SDK means absent
	// on the server; Terraform null preserves "computed-default" UX.
	if ws.TerraformVersion != "" {
		m.TerraformVersion = types.StringValue(ws.TerraformVersion)
	} else {
		m.TerraformVersion = types.StringNull()
	}
	if ws.VCSRepoURL != "" {
		m.VCSRepoURL = types.StringValue(ws.VCSRepoURL)
	} else {
		m.VCSRepoURL = types.StringNull()
	}
	if ws.VCSBranch != "" {
		m.VCSBranch = types.StringValue(ws.VCSBranch)
	} else {
		m.VCSBranch = types.StringNull()
	}
	if ws.AgentPoolID != "" {
		m.AgentPoolID = types.StringValue(ws.AgentPoolID)
	} else {
		m.AgentPoolID = types.StringNull()
	}
	if ws.VCSConnectionID != "" {
		m.VCSConnectionID = types.StringValue(ws.VCSConnectionID)
	} else {
		m.VCSConnectionID = types.StringNull()
	}

	// Drift detection
	m.DriftDetectionEnabled = types.BoolValue(ws.DriftDetectionEnabled)
	if ws.DriftDetectionIntervalSeconds != nil && *ws.DriftDetectionIntervalSeconds > 0 {
		m.DriftDetectionIntervalSeconds = types.Int64Value(*ws.DriftDetectionIntervalSeconds)
	} else {
		m.DriftDetectionIntervalSeconds = types.Int64Null()
	}
	if ws.DriftStatus != "" {
		m.DriftStatus = types.StringValue(ws.DriftStatus)
	} else {
		m.DriftStatus = types.StringNull()
	}
	if ws.DriftLastCheckedAt != "" {
		m.DriftLastCheckedAt = types.StringValue(ws.DriftLastCheckedAt)
	} else {
		m.DriftLastCheckedAt = types.StringNull()
	}
	if ws.DriftLatestRunID != "" {
		m.DriftLatestRunID = types.StringValue(ws.DriftLatestRunID)
	} else {
		m.DriftLatestRunID = types.StringNull()
	}

	// State + lifecycle + VCS poll status — read-only fields the server
	// surfaces for diagnostics and operator UX. Empty-string from the
	// SDK becomes Terraform null so a fresh workspace doesn't show
	// "empty string" values in the state diff.
	m.StateDiverged = types.BoolValue(ws.StateDiverged)
	if ws.LifecycleState != "" {
		m.LifecycleState = types.StringValue(ws.LifecycleState)
	} else {
		m.LifecycleState = types.StringNull()
	}
	if ws.LifecycleReason != "" {
		m.LifecycleReason = types.StringValue(ws.LifecycleReason)
	} else {
		m.LifecycleReason = types.StringNull()
	}
	if ws.VCSLastPolledAt != "" {
		m.VCSLastPolledAt = types.StringValue(ws.VCSLastPolledAt)
	} else {
		m.VCSLastPolledAt = types.StringNull()
	}
	if ws.VCSLastError != "" {
		m.VCSLastError = types.StringValue(ws.VCSLastError)
	} else {
		m.VCSLastError = types.StringNull()
	}
	if ws.VCSLastErrorAt != "" {
		m.VCSLastErrorAt = types.StringValue(ws.VCSLastErrorAt)
	} else {
		m.VCSLastErrorAt = types.StringNull()
	}
	if ws.AgentPoolName != "" {
		m.AgentPoolName = types.StringValue(ws.AgentPoolName)
	} else {
		m.AgentPoolName = types.StringNull()
	}
	if ws.VCSConnectionName != "" {
		m.VCSConnectionName = types.StringValue(ws.VCSConnectionName)
	} else {
		m.VCSConnectionName = types.StringNull()
	}

	// AI plan summary (#401). The server always returns a concrete
	// value for `ai-summary-mode` (defaulting to "default"); the
	// context is the empty string for new workspaces. Pin both to
	// concrete StringValues so Terraform doesn't see "unknown" drift.
	if ws.AISummaryMode != "" {
		m.AISummaryMode = types.StringValue(ws.AISummaryMode)
	} else {
		m.AISummaryMode = types.StringValue("default")
	}
	m.AISummaryContext = types.StringValue(ws.AISummaryContext)

	// Var files — same null-vs-empty rule as trigger_prefixes above.
	if m.VarFiles.IsNull() && len(ws.VarFiles) == 0 {
		m.VarFiles = types.ListNull(types.StringType)
	} else {
		vfVal, vfDiag := types.ListValueFrom(ctx, types.StringType, ws.VarFiles)
		diags.Append(vfDiag...)
		m.VarFiles = vfVal
	}

	// Trigger prefixes — repo paths beyond `working_directory` that must
	// land in the sparse-checkout fetch.
	//
	// The null-vs-empty-list ambiguity is the most-bitten edge in the
	// terraform-plugin-framework Optional list pattern, and we've been
	// burned by both directions:
	//
	// - v0.35.4: Read coerced `[]` to null. A caller that declared
	//   `trigger_prefixes = []` got plan=[] / apply=null mismatch.
	// - v0.35.5: Read coerced null to `[]`. A caller that OMITTED the
	//   field got plan=null / apply=[] mismatch on the very first
	//   apply against the new provider.
	//
	// Right answer: respect what the prior state holds. The framework
	// passes the prior state into this function via `m`, so checking
	// `m.TriggerPrefixes.IsNull()` BEFORE we overwrite it lets us
	// preserve null when the caller's config + prior state were both
	// null and the API just returned its default empty list. Only
	// materialise as `[]` when the prior state already had a non-null
	// list (or when the API returned a populated list).
	if m.TriggerPrefixes.IsNull() && len(ws.TriggerPrefixes) == 0 {
		// prior null + server empty → preserve null (caller omitted it)
		m.TriggerPrefixes = types.ListNull(types.StringType)
	} else {
		tpVal, tpDiag := types.ListValueFrom(ctx, types.StringType, ws.TriggerPrefixes)
		diags.Append(tpDiag...)
		m.TriggerPrefixes = tpVal
	}

	// Labels — same null-vs-empty-map rule as trigger_prefixes above.
	// `len(nil-map) == 0` so an empty server map collapses to the
	// preserve-null branch when prior state was null.
	if m.Labels.IsNull() && len(ws.Labels) == 0 {
		m.Labels = types.MapNull(types.StringType)
	} else {
		val, d := types.MapValueFrom(ctx, types.StringType, ws.Labels)
		diags.Append(d...)
		m.Labels = val
	}
	return diags
}

// buildWorkspaceAttrs converts the Terraform model into JSON:API attributes.
//
// DEPRECATED — kept for the ImportState path which still uses the raw
// client until the read-by-name path lands on go-terrapod. Will be
// deleted alongside the rest of provider/internal/client/ once every
// resource has migrated.
func buildWorkspaceAttrs(m *workspaceModel) map[string]any {
	attrs := map[string]any{
		"name": m.Name.ValueString(),
	}

	if !m.ExecutionMode.IsNull() && !m.ExecutionMode.IsUnknown() {
		attrs["execution-mode"] = m.ExecutionMode.ValueString()
	}
	if !m.AutoApply.IsNull() && !m.AutoApply.IsUnknown() {
		attrs["auto-apply"] = m.AutoApply.ValueBool()
	}
	if !m.ExecutionBackend.IsNull() && !m.ExecutionBackend.IsUnknown() {
		attrs["execution-backend"] = m.ExecutionBackend.ValueString()
	}
	if !m.TerraformVersion.IsNull() && !m.TerraformVersion.IsUnknown() {
		attrs["terraform-version"] = m.TerraformVersion.ValueString()
	}
	if !m.WorkingDirectory.IsNull() && !m.WorkingDirectory.IsUnknown() {
		attrs["working-directory"] = m.WorkingDirectory.ValueString()
	}
	if !m.ResourceCPU.IsNull() && !m.ResourceCPU.IsUnknown() {
		attrs["resource-cpu"] = m.ResourceCPU.ValueString()
	}
	if !m.ResourceMemory.IsNull() && !m.ResourceMemory.IsUnknown() {
		attrs["resource-memory"] = m.ResourceMemory.ValueString()
	}
	if !m.Labels.IsNull() && !m.Labels.IsUnknown() {
		labels := map[string]string{}
		for k, v := range m.Labels.Elements() {
			labels[k] = v.(types.String).ValueString()
		}
		attrs["labels"] = labels
	}
	if !m.VCSRepoURL.IsNull() {
		attrs["vcs-repo-url"] = m.VCSRepoURL.ValueString()
	}
	if !m.VCSBranch.IsNull() {
		attrs["vcs-branch"] = m.VCSBranch.ValueString()
	}
	if !m.VCSWorkflow.IsNull() && !m.VCSWorkflow.IsUnknown() {
		attrs["vcs-workflow"] = m.VCSWorkflow.ValueString()
	}
	if !m.AutoMerge.IsNull() && !m.AutoMerge.IsUnknown() {
		attrs["auto-merge"] = m.AutoMerge.ValueBool()
	}
	if !m.AutoMergeStrategy.IsNull() && !m.AutoMergeStrategy.IsUnknown() {
		attrs["auto-merge-strategy"] = m.AutoMergeStrategy.ValueString()
	}
	if !m.AgentPoolID.IsNull() {
		attrs["agent-pool-id"] = m.AgentPoolID.ValueString()
	}
	if !m.VarFiles.IsNull() && !m.VarFiles.IsUnknown() {
		varFiles := []string{}
		for _, v := range m.VarFiles.Elements() {
			varFiles = append(varFiles, v.(types.String).ValueString())
		}
		attrs["var-files"] = varFiles
	}
	if !m.DriftDetectionEnabled.IsNull() && !m.DriftDetectionEnabled.IsUnknown() {
		attrs["drift-detection-enabled"] = m.DriftDetectionEnabled.ValueBool()
	}
	if !m.DriftDetectionIntervalSeconds.IsNull() && !m.DriftDetectionIntervalSeconds.IsUnknown() {
		attrs["drift-detection-interval-seconds"] = m.DriftDetectionIntervalSeconds.ValueInt64()
	}

	return attrs
}

// buildWorkspaceRels converts the Terraform model into JSON:API relationships.
func buildWorkspaceRels(m *workspaceModel) map[string]any {
	if m.VCSConnectionID.IsNull() || m.VCSConnectionID.ValueString() == "" {
		return nil
	}
	return map[string]any{
		"vcs-connection": map[string]any{
			"data": map[string]any{
				"id":   m.VCSConnectionID.ValueString(),
				"type": "vcs-connections",
			},
		},
	}
}

// readResourceIntoModel populates the Terraform model from a JSON:API resource.
func readResourceIntoModel(ctx context.Context, res *terrapod.Resource, m *workspaceModel) diag.Diagnostics {
	var diags diag.Diagnostics

	m.ID = types.StringValue(res.ID)
	m.Name = types.StringValue(terrapod.GetStringAttr(res, "name"))
	m.ExecutionMode = types.StringValue(terrapod.GetStringAttr(res, "execution-mode"))
	m.AutoApply = types.BoolValue(terrapod.GetBoolAttr(res, "auto-apply"))
	m.ExecutionBackend = types.StringValue(terrapod.GetStringAttr(res, "execution-backend"))
	m.WorkingDirectory = types.StringValue(terrapod.GetStringAttr(res, "working-directory"))
	m.ResourceCPU = types.StringValue(terrapod.GetStringAttr(res, "resource-cpu"))
	m.ResourceMemory = types.StringValue(terrapod.GetStringAttr(res, "resource-memory"))
	m.VCSWorkflow = types.StringValue(terrapod.GetStringAttr(res, "vcs-workflow"))
	m.AutoMerge = types.BoolValue(terrapod.GetBoolAttr(res, "auto-merge"))
	m.AutoMergeStrategy = types.StringValue(terrapod.GetStringAttr(res, "auto-merge-strategy"))
	m.OwnerEmail = types.StringValue(terrapod.GetStringAttr(res, "owner-email"))
	m.Locked = types.BoolValue(terrapod.GetBoolAttr(res, "locked"))
	m.CreatedAt = types.StringValue(terrapod.GetStringAttr(res, "created-at"))
	m.UpdatedAt = types.StringValue(terrapod.GetStringAttr(res, "updated-at"))

	// Nullable string fields
	if v := terrapod.GetStringAttr(res, "terraform-version"); v != "" {
		m.TerraformVersion = types.StringValue(v)
	} else {
		m.TerraformVersion = types.StringNull()
	}

	if v := terrapod.GetStringAttr(res, "vcs-repo-url"); v != "" {
		m.VCSRepoURL = types.StringValue(v)
	} else {
		m.VCSRepoURL = types.StringNull()
	}
	if v := terrapod.GetStringAttr(res, "vcs-branch"); v != "" {
		m.VCSBranch = types.StringValue(v)
	} else {
		m.VCSBranch = types.StringNull()
	}
	if v := terrapod.GetStringAttr(res, "agent-pool-id"); v != "" {
		m.AgentPoolID = types.StringValue(v)
	} else {
		m.AgentPoolID = types.StringNull()
	}

	// VCS connection from relationship
	if v := terrapod.GetRelationshipID(res, "vcs-connection"); v != "" {
		m.VCSConnectionID = types.StringValue(v)
	} else {
		m.VCSConnectionID = types.StringNull()
	}

	// Drift detection
	m.DriftDetectionEnabled = types.BoolValue(terrapod.GetBoolAttr(res, "drift-detection-enabled"))
	if v := terrapod.GetIntAttr(res, "drift-detection-interval-seconds"); v > 0 {
		m.DriftDetectionIntervalSeconds = types.Int64Value(v)
	} else {
		m.DriftDetectionIntervalSeconds = types.Int64Null()
	}
	if v := terrapod.GetStringAttr(res, "drift-status"); v != "" {
		m.DriftStatus = types.StringValue(v)
	} else {
		m.DriftStatus = types.StringNull()
	}
	if v := terrapod.GetStringAttr(res, "drift-last-checked-at"); v != "" {
		m.DriftLastCheckedAt = types.StringValue(v)
	} else {
		m.DriftLastCheckedAt = types.StringNull()
	}

	// Var files
	if varFiles := terrapod.GetListAttr(res, "var-files"); len(varFiles) > 0 {
		val, d := types.ListValueFrom(ctx, types.StringType, varFiles)
		diags.Append(d...)
		m.VarFiles = val
	} else {
		m.VarFiles = types.ListNull(types.StringType)
	}

	// Labels
	if labels := terrapod.GetMapAttr(res, "labels"); len(labels) > 0 {
		val, d := types.MapValueFrom(ctx, types.StringType, labels)
		diags.Append(d...)
		m.Labels = val
	} else {
		m.Labels = types.MapNull(types.StringType)
	}

	return diags
}

// putRemoteStateConsumers declaratively replaces the producer's full
// consumer set via the #344 PUT endpoint. Empty `ids` means "remove
// all". Server-side enforces admin on the producer.
func putRemoteStateConsumers(ctx context.Context, c *terrapod.Client, workspaceID string, ids []string) error {
	items := make([]map[string]any, len(ids))
	for i, id := range ids {
		items[i] = map[string]any{"type": "workspaces", "id": id}
	}
	body, err := json.Marshal(map[string]any{"data": items})
	if err != nil {
		return err
	}
	_, err = c.Put(ctx, fmt.Sprintf("/api/terrapod/v1/workspaces/%s/remote-state-consumers", workspaceID), body)
	return err
}

// readRemoteStateConsumers reads the producer's outbound consumer
// workspace IDs and returns them as a terraform Set<string>. A null
// Set is returned on error (caller decides whether to surface it).
func readRemoteStateConsumers(ctx context.Context, c *terrapod.Client, workspaceID string) (types.Set, diag.Diagnostics) {
	var diags diag.Diagnostics
	url := fmt.Sprintf("/api/terrapod/v1/workspaces/%s/remote-state-consumers?filter[remote-state-consumer][type]=outbound", workspaceID)
	data, err := c.Get(ctx, url)
	if err != nil {
		diags.AddError("Failed to read remote_state_consumers", err.Error())
		return types.SetNull(types.StringType), diags
	}
	items, err := terrapod.ParseResourceList(data)
	if err != nil {
		diags.AddError("Failed to parse remote_state_consumers response", err.Error())
		return types.SetNull(types.StringType), diags
	}
	vals := make([]attr.Value, 0, len(items))
	for i := range items {
		if v := terrapod.GetRelationshipID(&items[i], "consumer"); v != "" {
			vals = append(vals, types.StringValue(v))
		}
	}
	s, d := types.SetValue(types.StringType, vals)
	diags.Append(d...)
	return s, diags
}

// applyConsumersFromPlan PUTs the plan's remote_state_consumers to the
// server iff the attribute is non-null. Null in plan ⇒ unmanaged here
// (server side left intact). Empty set ⇒ explicit "remove all".
func applyConsumersFromPlan(ctx context.Context, c *terrapod.Client, workspaceID string, plan types.Set) error {
	if plan.IsNull() || plan.IsUnknown() {
		return nil
	}
	elems := plan.Elements()
	ids := make([]string, 0, len(elems))
	for _, v := range elems {
		s, ok := v.(types.String)
		if !ok || s.IsNull() || s.IsUnknown() {
			continue
		}
		ids = append(ids, s.ValueString())
	}
	return putRemoteStateConsumers(ctx, c, workspaceID, ids)
}
