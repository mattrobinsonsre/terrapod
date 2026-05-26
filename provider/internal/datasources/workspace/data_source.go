// Package workspace implements the terrapod_workspace data source.
//
// API Contract: GET /api/v2/organizations/default/workspaces/{name}
// Looks up a single workspace by name. Returns all workspace attributes.
// See resources/workspace/model.go for full attribute mapping.
package workspace

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var _ datasource.DataSource = &workspaceDataSource{}

type workspaceDataSource struct {
	tc *terrapod.Client
}

type workspaceDataSourceModel struct {
	ID                            types.String `tfsdk:"id"`
	Name                          types.String `tfsdk:"name"`
	ExecutionMode                 types.String `tfsdk:"execution_mode"`
	AutoApply                     types.Bool   `tfsdk:"auto_apply"`
	ExecutionBackend              types.String `tfsdk:"execution_backend"`
	TerraformVersion              types.String `tfsdk:"terraform_version"`
	WorkingDirectory              types.String `tfsdk:"working_directory"`
	ResourceCPU                   types.String `tfsdk:"resource_cpu"`
	ResourceMemory                types.String `tfsdk:"resource_memory"`
	Labels                        types.Map    `tfsdk:"labels"`
	VCSRepoURL                    types.String `tfsdk:"vcs_repo_url"`
	VCSBranch                     types.String `tfsdk:"vcs_branch"`
	VCSConnectionID               types.String `tfsdk:"vcs_connection_id"`
	AgentPoolID                   types.String `tfsdk:"agent_pool_id"`
	VarFiles                      types.List   `tfsdk:"var_files"`
	DriftDetectionEnabled         types.Bool   `tfsdk:"drift_detection_enabled"`
	DriftDetectionIntervalSeconds types.Int64  `tfsdk:"drift_detection_interval_seconds"`
	OwnerEmail                    types.String `tfsdk:"owner_email"`
	DriftStatus                   types.String `tfsdk:"drift_status"`
	DriftLastCheckedAt            types.String `tfsdk:"drift_last_checked_at"`
	LifecycleState                types.String `tfsdk:"lifecycle_state"`
	LifecycleReason               types.String `tfsdk:"lifecycle_reason"`
	Locked                        types.Bool   `tfsdk:"locked"`
	CreatedAt                     types.String `tfsdk:"created_at"`
	UpdatedAt                     types.String `tfsdk:"updated_at"`
}

// NewDataSource returns a new workspace data source.
func NewDataSource() datasource.DataSource {
	return &workspaceDataSource{}
}

func (d *workspaceDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_workspace"
}

func (d *workspaceDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Look up a Terrapod workspace by name.",
		Attributes: map[string]schema.Attribute{
			"id":                               computedString("Workspace ID."),
			"name":                             requiredString("Workspace name to look up."),
			"execution_mode":                   computedString("Execution mode."),
			"auto_apply":                       computedBool("Auto-apply setting."),
			"execution_backend":                computedString("Execution backend."),
			"terraform_version":                computedString("Terraform/tofu version."),
			"working_directory":                computedString("Working directory."),
			"resource_cpu":                     computedString("CPU request."),
			"resource_memory":                  computedString("Memory request."),
			"labels":                           computedMap("Labels."),
			"vcs_repo_url":                     computedString("VCS repo URL."),
			"vcs_branch":                       computedString("VCS branch."),
			"vcs_connection_id":                computedString("VCS connection ID."),
			"agent_pool_id":                    computedString("Agent pool ID."),
			"var_files":                        computedList("Var files for -var-file arguments."),
			"drift_detection_enabled":          computedBool("Drift detection enabled."),
			"drift_detection_interval_seconds": computedInt64("Drift detection interval."),
			"owner_email":                      computedString("Owner email."),
			"drift_status":                     computedString("Drift status."),
			"drift_last_checked_at":            computedString("Last drift check."),
			"lifecycle_state":                  computedString("Workspace lifecycle state (e.g. active, or flagged/destroying when its autodiscovery source directory was deleted)."),
			"lifecycle_reason":                 computedString("Human-readable reason for the current lifecycle state."),
			"locked":                           computedBool("Lock status."),
			"created_at":                       computedString("Creation timestamp."),
			"updated_at":                       computedString("Update timestamp."),
		},
	}
}

func (d *workspaceDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	c, ok := req.ProviderData.(*client.Client)
	if !ok {
		resp.Diagnostics.AddError("Unexpected provider data type", fmt.Sprintf("Expected *client.Client, got %T", req.ProviderData))
		return
	}
	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: c.BaseURL, Token: c.Token})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	d.tc = tc
}

func (d *workspaceDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config workspaceDataSourceModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	data, err := d.tc.Get(ctx, "/api/v2/organizations/default/workspaces/"+config.Name.ValueString())
	if err != nil {
		resp.Diagnostics.AddError("Failed to read workspace", err.Error())
		return
	}

	res, err := terrapod.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	resp.Diagnostics.Append(readDataSourceModel(ctx, res, &config)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &config)...)
}

func readDataSourceModel(ctx context.Context, res *terrapod.Resource, m *workspaceDataSourceModel) diag.Diagnostics {
	var diags diag.Diagnostics

	m.ID = types.StringValue(res.ID)
	m.Name = types.StringValue(terrapod.GetStringAttr(res, "name"))
	m.ExecutionMode = types.StringValue(terrapod.GetStringAttr(res, "execution-mode"))
	m.AutoApply = types.BoolValue(terrapod.GetBoolAttr(res, "auto-apply"))
	m.ExecutionBackend = types.StringValue(terrapod.GetStringAttr(res, "execution-backend"))
	m.WorkingDirectory = types.StringValue(terrapod.GetStringAttr(res, "working-directory"))
	m.ResourceCPU = types.StringValue(terrapod.GetStringAttr(res, "resource-cpu"))
	m.ResourceMemory = types.StringValue(terrapod.GetStringAttr(res, "resource-memory"))
	m.OwnerEmail = types.StringValue(terrapod.GetStringAttr(res, "owner-email"))
	m.Locked = types.BoolValue(terrapod.GetBoolAttr(res, "locked"))
	m.CreatedAt = types.StringValue(terrapod.GetStringAttr(res, "created-at"))
	m.UpdatedAt = types.StringValue(terrapod.GetStringAttr(res, "updated-at"))
	m.DriftDetectionEnabled = types.BoolValue(terrapod.GetBoolAttr(res, "drift-detection-enabled"))

	setOptionalString(&m.TerraformVersion, terrapod.GetStringAttr(res, "terraform-version"))
	setOptionalString(&m.VCSRepoURL, terrapod.GetStringAttr(res, "vcs-repo-url"))
	setOptionalString(&m.VCSBranch, terrapod.GetStringAttr(res, "vcs-branch"))
	setOptionalString(&m.AgentPoolID, terrapod.GetStringAttr(res, "agent-pool-id"))
	setOptionalString(&m.DriftStatus, terrapod.GetStringAttr(res, "drift-status"))
	setOptionalString(&m.DriftLastCheckedAt, terrapod.GetStringAttr(res, "drift-last-checked-at"))
	setOptionalString(&m.LifecycleState, terrapod.GetStringAttr(res, "lifecycle-state"))
	setOptionalString(&m.LifecycleReason, terrapod.GetStringAttr(res, "lifecycle-reason"))

	if v := terrapod.GetRelationshipID(res, "vcs-connection"); v != "" {
		m.VCSConnectionID = types.StringValue(v)
	} else {
		m.VCSConnectionID = types.StringNull()
	}

	if v := terrapod.GetIntAttr(res, "drift-detection-interval-seconds"); v > 0 {
		m.DriftDetectionIntervalSeconds = types.Int64Value(v)
	} else {
		m.DriftDetectionIntervalSeconds = types.Int64Null()
	}

	if varFiles := terrapod.GetListAttr(res, "var-files"); len(varFiles) > 0 {
		val, d := types.ListValueFrom(ctx, types.StringType, varFiles)
		diags.Append(d...)
		m.VarFiles = val
	} else {
		m.VarFiles = types.ListNull(types.StringType)
	}

	if labels := terrapod.GetMapAttr(res, "labels"); len(labels) > 0 {
		val, d := types.MapValueFrom(ctx, types.StringType, labels)
		diags.Append(d...)
		m.Labels = val
	} else {
		m.Labels = types.MapNull(types.StringType)
	}

	return diags
}

func setOptionalString(target *types.String, value string) {
	if value != "" {
		*target = types.StringValue(value)
	} else {
		*target = types.StringNull()
	}
}

// Schema helpers
func computedString(desc string) schema.StringAttribute {
	return schema.StringAttribute{Description: desc, Computed: true}
}
func requiredString(desc string) schema.StringAttribute {
	return schema.StringAttribute{Description: desc, Required: true}
}
func computedBool(desc string) schema.BoolAttribute {
	return schema.BoolAttribute{Description: desc, Computed: true}
}
func computedInt64(desc string) schema.Int64Attribute {
	return schema.Int64Attribute{Description: desc, Computed: true}
}
func computedList(desc string) schema.ListAttribute {
	return schema.ListAttribute{Description: desc, Computed: true, ElementType: types.StringType}
}
func computedMap(desc string) schema.MapAttribute {
	return schema.MapAttribute{Description: desc, Computed: true, ElementType: types.StringType}
}
