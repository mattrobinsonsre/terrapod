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

	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var _ datasource.DataSource = &workspaceDataSource{}

type workspaceDataSource struct {
	client *client.Client
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
	VCSWorkingDirectory           types.String `tfsdk:"vcs_working_directory"`
	VCSConnectionID               types.String `tfsdk:"vcs_connection_id"`
	AgentPoolID                   types.String `tfsdk:"agent_pool_id"`
	VarFiles                      types.List   `tfsdk:"var_files"`
	DriftDetectionEnabled         types.Bool   `tfsdk:"drift_detection_enabled"`
	DriftDetectionIntervalSeconds types.Int64  `tfsdk:"drift_detection_interval_seconds"`
	OwnerEmail                    types.String `tfsdk:"owner_email"`
	DriftStatus                   types.String `tfsdk:"drift_status"`
	DriftLastCheckedAt            types.String `tfsdk:"drift_last_checked_at"`
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
			"id":                                computedString("Workspace ID."),
			"name":                              requiredString("Workspace name to look up."),
			"execution_mode":                    computedString("Execution mode."),
			"auto_apply":                        computedBool("Auto-apply setting."),
			"execution_backend":                 computedString("Execution backend."),
			"terraform_version":                 computedString("Terraform/tofu version."),
			"working_directory":                 computedString("Working directory."),
			"resource_cpu":                      computedString("CPU request."),
			"resource_memory":                   computedString("Memory request."),
			"labels":                            computedMap("Labels."),
			"vcs_repo_url":                      computedString("VCS repo URL."),
			"vcs_branch":                        computedString("VCS branch."),
			"vcs_working_directory":             computedString("VCS working directory."),
			"vcs_connection_id":                 computedString("VCS connection ID."),
			"agent_pool_id":                     computedString("Agent pool ID."),
			"var_files":                         computedList("Var files for -var-file arguments."),
			"drift_detection_enabled":           computedBool("Drift detection enabled."),
			"drift_detection_interval_seconds":  computedInt64("Drift detection interval."),
			"owner_email":                       computedString("Owner email."),
			"drift_status":                      computedString("Drift status."),
			"drift_last_checked_at":             computedString("Last drift check."),
			"locked":                            computedBool("Lock status."),
			"created_at":                        computedString("Creation timestamp."),
			"updated_at":                        computedString("Update timestamp."),
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
	d.client = c
}

func (d *workspaceDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config workspaceDataSourceModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	data, err := d.client.Get(ctx, "/api/v2/organizations/default/workspaces/"+config.Name.ValueString())
	if err != nil {
		resp.Diagnostics.AddError("Failed to read workspace", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	resp.Diagnostics.Append(readDataSourceModel(ctx, res, &config)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &config)...)
}

func readDataSourceModel(ctx context.Context, res *client.Resource, m *workspaceDataSourceModel) diag.Diagnostics {
	var diags diag.Diagnostics

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
	m.DriftDetectionEnabled = types.BoolValue(client.GetBoolAttr(res, "drift-detection-enabled"))

	setOptionalString(&m.TerraformVersion, client.GetStringAttr(res, "terraform-version"))
	setOptionalString(&m.VCSRepoURL, client.GetStringAttr(res, "vcs-repo-url"))
	setOptionalString(&m.VCSBranch, client.GetStringAttr(res, "vcs-branch"))
	setOptionalString(&m.VCSWorkingDirectory, client.GetStringAttr(res, "vcs-working-directory"))
	setOptionalString(&m.AgentPoolID, client.GetStringAttr(res, "agent-pool-id"))
	setOptionalString(&m.DriftStatus, client.GetStringAttr(res, "drift-status"))
	setOptionalString(&m.DriftLastCheckedAt, client.GetStringAttr(res, "drift-last-checked-at"))

	if v := client.GetRelationshipID(res, "vcs-connection"); v != "" {
		m.VCSConnectionID = types.StringValue(v)
	} else {
		m.VCSConnectionID = types.StringNull()
	}

	if v := client.GetIntAttr(res, "drift-detection-interval-seconds"); v > 0 {
		m.DriftDetectionIntervalSeconds = types.Int64Value(v)
	} else {
		m.DriftDetectionIntervalSeconds = types.Int64Null()
	}

	if varFiles := client.GetListAttr(res, "var-files"); len(varFiles) > 0 {
		val, d := types.ListValueFrom(ctx, types.StringType, varFiles)
		diags.Append(d...)
		m.VarFiles = val
	} else {
		m.VarFiles = types.ListNull(types.StringType)
	}

	if labels := client.GetMapAttr(res, "labels"); len(labels) > 0 {
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
