package workspace

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var (
	_ resource.Resource                = &workspaceResource{}
	_ resource.ResourceWithImportState = &workspaceResource{}
)

type workspaceResource struct {
	client *client.Client
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
				Description: "The workspace name. Changing this forces a new resource.",
				Required:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"execution_mode": schema.StringAttribute{
				Description: "Execution mode: local, remote, or agent.",
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
			},
			"working_directory": schema.StringAttribute{
				Description: "Working directory relative to the repo root.",
				Optional:    true,
				Computed:    true,
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
			"vcs_working_directory": schema.StringAttribute{
				Description: "Subdirectory within the repo.",
				Optional:    true,
			},
			"vcs_connection_id": schema.StringAttribute{
				Description: "VCS connection ID (e.g. vcs-abc123).",
				Optional:    true,
			},
			"agent_pool_id": schema.StringAttribute{
				Description: "Agent pool ID for agent execution mode.",
				Optional:    true,
			},
			"drift_detection_enabled": schema.BoolAttribute{
				Description: "Enable drift detection for this workspace.",
				Optional:    true,
				Computed:    true,
				Default:     booldefault.StaticBool(false),
			},
			"drift_detection_interval_seconds": schema.Int64Attribute{
				Description: "Interval in seconds between drift detection checks.",
				Optional:    true,
				Computed:    true,
			},

			// Read-only
			"owner_email": schema.StringAttribute{
				Description: "Email of the workspace owner.",
				Computed:    true,
			},
			"drift_status": schema.StringAttribute{
				Description: "Current drift status.",
				Computed:    true,
			},
			"drift_last_checked_at": schema.StringAttribute{
				Description: "Timestamp of the last drift check.",
				Computed:    true,
			},
			"locked": schema.BoolAttribute{
				Description: "Whether the workspace is locked.",
				Computed:    true,
			},
			"created_at": schema.StringAttribute{
				Description: "Creation timestamp.",
				Computed:    true,
			},
			"updated_at": schema.StringAttribute{
				Description: "Last update timestamp.",
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
}

func (r *workspaceResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan workspaceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs := buildWorkspaceAttrs(&plan)
	rels := buildWorkspaceRels(&plan)

	body, err := client.MarshalResource("workspaces", attrs, rels)
	if err != nil {
		resp.Diagnostics.AddError("Failed to marshal request", err.Error())
		return
	}

	data, err := r.client.Post(ctx, "/api/v2/organizations/default/workspaces", body)
	if err != nil {
		resp.Diagnostics.AddError("Failed to create workspace", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	readResourceIntoModel(res, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *workspaceResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state workspaceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	data, err := r.client.Get(ctx, "/api/v2/workspaces/"+state.ID.ValueString())
	if err != nil {
		if client.IsNotFound(err) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Failed to read workspace", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	readResourceIntoModel(res, &state)
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

	attrs := buildWorkspaceAttrs(&plan)
	body, err := client.MarshalResourceWithID(state.ID.ValueString(), "workspaces", attrs)
	if err != nil {
		resp.Diagnostics.AddError("Failed to marshal request", err.Error())
		return
	}

	data, err := r.client.Patch(ctx, "/api/v2/workspaces/"+state.ID.ValueString(), body)
	if err != nil {
		resp.Diagnostics.AddError("Failed to update workspace", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	readResourceIntoModel(res, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *workspaceResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state workspaceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	err := r.client.Delete(ctx, "/api/v2/workspaces/"+state.ID.ValueString())
	if err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Failed to delete workspace", err.Error())
	}
}

func (r *workspaceResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	// Import by workspace name — resolve to ID via the by-name endpoint.
	name := req.ID

	data, err := r.client.Get(ctx, "/api/v2/organizations/default/workspaces/"+name)
	if err != nil {
		resp.Diagnostics.AddError("Failed to import workspace", fmt.Sprintf("Could not find workspace %q: %s", name, err))
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), res.ID)...)
}

// buildWorkspaceAttrs converts the Terraform model into JSON:API attributes.
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
	if !m.VCSWorkingDirectory.IsNull() {
		attrs["vcs-working-directory"] = m.VCSWorkingDirectory.ValueString()
	}
	if !m.AgentPoolID.IsNull() {
		attrs["agent-pool-id"] = m.AgentPoolID.ValueString()
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
func readResourceIntoModel(res *client.Resource, m *workspaceModel) {
	m.ID = types.StringValue(res.ID)
	m.Name = types.StringValue(client.GetStringAttr(res, "name"))
	m.ExecutionMode = types.StringValue(client.GetStringAttr(res, "execution-mode"))
	m.AutoApply = types.BoolValue(client.GetBoolAttr(res, "auto-apply"))
	m.ExecutionBackend = types.StringValue(client.GetStringAttr(res, "execution-backend"))
	m.WorkingDirectory = types.StringValue(client.GetStringAttr(res, "working-directory"))
	m.ResourceCPU = types.StringValue(client.GetStringAttr(res, "resource-cpu"))
	m.ResourceMemory = types.StringValue(client.GetStringAttr(res, "resource-memory"))
	m.OwnerEmail = types.StringValue(client.GetStringAttr(res, "owner-email"))
	m.Locked = types.BoolValue(client.GetBoolAttr(res, "locked"))
	m.CreatedAt = types.StringValue(client.GetStringAttr(res, "created-at"))
	m.UpdatedAt = types.StringValue(client.GetStringAttr(res, "updated-at"))

	// Nullable string fields
	if v := client.GetStringAttr(res, "terraform-version"); v != "" {
		m.TerraformVersion = types.StringValue(v)
	} else {
		m.TerraformVersion = types.StringNull()
	}

	if v := client.GetStringAttr(res, "vcs-repo-url"); v != "" {
		m.VCSRepoURL = types.StringValue(v)
	} else {
		m.VCSRepoURL = types.StringNull()
	}
	if v := client.GetStringAttr(res, "vcs-branch"); v != "" {
		m.VCSBranch = types.StringValue(v)
	} else {
		m.VCSBranch = types.StringNull()
	}
	if v := client.GetStringAttr(res, "vcs-working-directory"); v != "" {
		m.VCSWorkingDirectory = types.StringValue(v)
	} else {
		m.VCSWorkingDirectory = types.StringNull()
	}
	if v := client.GetStringAttr(res, "agent-pool-id"); v != "" {
		m.AgentPoolID = types.StringValue(v)
	} else {
		m.AgentPoolID = types.StringNull()
	}

	// VCS connection from relationship
	if v := client.GetRelationshipID(res, "vcs-connection"); v != "" {
		m.VCSConnectionID = types.StringValue(v)
	} else {
		m.VCSConnectionID = types.StringNull()
	}

	// Drift detection
	m.DriftDetectionEnabled = types.BoolValue(client.GetBoolAttr(res, "drift-detection-enabled"))
	if v := client.GetIntAttr(res, "drift-detection-interval-seconds"); v > 0 {
		m.DriftDetectionIntervalSeconds = types.Int64Value(v)
	} else {
		m.DriftDetectionIntervalSeconds = types.Int64Null()
	}
	if v := client.GetStringAttr(res, "drift-status"); v != "" {
		m.DriftStatus = types.StringValue(v)
	} else {
		m.DriftStatus = types.StringNull()
	}
	if v := client.GetStringAttr(res, "drift-last-checked-at"); v != "" {
		m.DriftLastCheckedAt = types.StringValue(v)
	} else {
		m.DriftLastCheckedAt = types.StringNull()
	}

	// Labels
	labels := client.GetMapAttr(res, "labels")
	if labels != nil && len(labels) > 0 {
		elems := make(map[string]types.String, len(labels))
		for k, v := range labels {
			elems[k] = types.StringValue(v)
		}
		// This is a simplified approach; in production you'd use types.MapValueFrom
		m.Labels, _ = types.MapValueFrom(context.Background(), types.StringType, labels)
	} else {
		m.Labels = types.MapNull(types.StringType)
	}
}
