// Package variable_set implements the terrapod_variable_set resource.
// Migrated to go-terrapod (#347).
package variable_set

import (
	"context"
	"errors"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

type variableSetModel struct {
	ID types.String `tfsdk:"id"`

	Name        types.String `tfsdk:"name"`
	Description types.String `tfsdk:"description"`
	Global      types.Bool   `tfsdk:"global"`
	Priority    types.Bool   `tfsdk:"priority"`

	AssignmentRule *assignmentRuleModel `tfsdk:"assignment_rule"`

	VarCount       types.Int64  `tfsdk:"var_count"`
	WorkspaceCount types.Int64  `tfsdk:"workspace_count"`
	CreatedAt      types.String `tfsdk:"created_at"`
	UpdatedAt      types.String `tfsdk:"updated_at"`
}

// assignmentRuleModel mirrors the server's workspace selector exactly.
//
// Every dimension the API can store is modelled here deliberately: a field the
// server persists but the provider does not read comes back absent on the next
// Read and shows up as permanent plan drift. The one dimension left out is
// `all` and `workspace_ids`, which the API refuses to store at all — a set that
// applies everywhere is expressed as `global`, and a literal list of ids is
// explicit assignment rather than a rule.
type assignmentRuleModel struct {
	Labels           map[string]string `tfsdk:"labels"`
	NamePrefix       types.String      `tfsdk:"name_prefix"`
	NameGlob         types.String      `tfsdk:"name_glob"`
	ExecutionBackend types.String      `tfsdk:"execution_backend"`
	ExecutionMode    types.String      `tfsdk:"execution_mode"`
	TerraformVersion types.String      `tfsdk:"terraform_version"`
	AgentPoolID      types.String      `tfsdk:"agent_pool_id"`
	VCSConnectionID  types.String      `tfsdk:"vcs_connection_id"`
	OwnerEmail       types.String      `tfsdk:"owner_email"`
	DriftStatus      types.String      `tfsdk:"drift_status"`
	Locked           types.Bool        `tfsdk:"locked"`
	HasVCS           types.Bool        `tfsdk:"has_vcs"`
}

var (
	_ resource.Resource                = &variableSetResource{}
	_ resource.ResourceWithImportState = &variableSetResource{}
)

type variableSetResource struct {
	client *client.Client
	tc     *terrapod.Client
}

func NewResource() resource.Resource { return &variableSetResource{} }

func (r *variableSetResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_variable_set"
}

func (r *variableSetResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a variable set in Terrapod.",
		Attributes: map[string]schema.Attribute{
			"id":          schema.StringAttribute{Computed: true, Description: "Variable set ID.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"name":        schema.StringAttribute{Required: true, Description: "The variable set name."},
			"description": schema.StringAttribute{Optional: true, Description: "Description of the variable set."},
			"global":      schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(false), Description: "Apply this variable set to all workspaces."},
			"priority":    schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(false), Description: "Priority variable sets override workspace variables."},
			"assignment_rule": schema.SingleNestedAttribute{
				Optional: true,
				Description: "Select workspaces by their attributes instead of assigning them one by one. " +
					"All dimensions given are AND-combined, and membership is re-evaluated on every run, " +
					"so a workspace that later matches picks the set up without being touched. " +
					"Conflicts with `global`, which already applies to every workspace. " +
					"Note that `workspace_count` reflects only explicit assignments and does not " +
					"count workspaces matched by this rule.",
				Attributes: map[string]schema.Attribute{
					"labels":            schema.MapAttribute{Optional: true, ElementType: types.StringType, Description: "Match workspaces carrying all of these labels."},
					"name_prefix":       schema.StringAttribute{Optional: true, Description: "Match workspaces whose name starts with this."},
					"name_glob":         schema.StringAttribute{Optional: true, Description: "Match workspace names against a `*`/`?` glob."},
					"execution_backend": schema.StringAttribute{Optional: true, Description: "Match the execution backend (terraform or tofu)."},
					"execution_mode":    schema.StringAttribute{Optional: true, Description: "Match the execution mode (local or agent)."},
					"terraform_version": schema.StringAttribute{Optional: true, Description: "Match the configured Terraform/OpenTofu version."},
					"agent_pool_id":     schema.StringAttribute{Optional: true, Description: "Match workspaces using this agent pool."},
					"vcs_connection_id": schema.StringAttribute{Optional: true, Description: "Match workspaces using this VCS connection."},
					"owner_email":       schema.StringAttribute{Optional: true, Description: "Match workspaces with this owner."},
					"drift_status":      schema.StringAttribute{Optional: true, Description: "Match the current drift status."},
					"locked":            schema.BoolAttribute{Optional: true, Description: "Match workspaces by lock state."},
					"has_vcs":           schema.BoolAttribute{Optional: true, Description: "Match workspaces by whether they are VCS-connected."},
				},
			},
			"var_count":       schema.Int64Attribute{Computed: true, Description: "Number of variables in this set."},
			"workspace_count": schema.Int64Attribute{Computed: true, Description: "Number of workspaces explicitly assigned to this set. Does not include workspaces matched by `assignment_rule`, or every workspace when `global` is set."},
			"created_at":      schema.StringAttribute{Computed: true, Description: "Creation timestamp.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"updated_at":      schema.StringAttribute{Computed: true, Description: "Update timestamp."},
		},
	}
}

func (r *variableSetResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	c, ok := req.ProviderData.(*client.Client)
	if !ok {
		resp.Diagnostics.AddError("Unexpected provider data type", fmt.Sprintf("Expected *client.Client, got %T", req.ProviderData))
		return
	}
	r.client = c
	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: c.BaseURL, Token: c.Token})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	r.tc = tc
}

func (r *variableSetResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan variableSetModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	v, err := r.tc.CreateVariableSet(ctx, buildCreateVarsetRequest(&plan))
	if err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}
	readVarsetFromSDK(v, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *variableSetResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state variableSetModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	v, err := r.tc.GetVariableSet(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}
	readVarsetFromSDK(v, &state)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *variableSetResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan variableSetModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var state variableSetModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	v, err := r.tc.UpdateVariableSet(ctx, state.ID.ValueString(), buildUpdateVarsetRequest(&plan))
	if err != nil {
		resp.Diagnostics.AddError("Update failed", err.Error())
		return
	}
	readVarsetFromSDK(v, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *variableSetResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state variableSetModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	err := r.tc.DeleteVariableSet(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Delete failed", err.Error())
		}
	}
}

func (r *variableSetResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

// assignmentRuleToAPI flattens the typed rule to the map the API stores.
//
// Only fields the operator actually set are emitted. An empty rule sends
// nothing at all rather than `{}`, because an empty filter is one the API
// rejects — and it would mean "match everything" if it did not.
func assignmentRuleToAPI(m *assignmentRuleModel) map[string]any {
	if m == nil {
		return nil
	}
	out := map[string]any{}
	putStr := func(key string, v types.String) {
		if !v.IsNull() && !v.IsUnknown() && v.ValueString() != "" {
			out[key] = v.ValueString()
		}
	}
	putBool := func(key string, v types.Bool) {
		if !v.IsNull() && !v.IsUnknown() {
			out[key] = v.ValueBool()
		}
	}
	if len(m.Labels) > 0 {
		out["labels"] = m.Labels
	}
	putStr("name_prefix", m.NamePrefix)
	putStr("name_glob", m.NameGlob)
	putStr("execution_backend", m.ExecutionBackend)
	putStr("execution_mode", m.ExecutionMode)
	putStr("terraform_version", m.TerraformVersion)
	putStr("agent_pool_id", m.AgentPoolID)
	putStr("vcs_connection_id", m.VCSConnectionID)
	putStr("owner_email", m.OwnerEmail)
	putStr("drift_status", m.DriftStatus)
	putBool("locked", m.Locked)
	putBool("has_vcs", m.HasVCS)
	if len(out) == 0 {
		return nil
	}
	return out
}

// assignmentRuleFromAPI is the exact inverse, so a rule round-trips unchanged.
//
// A field the server holds but this function drops would come back absent on
// every Read and produce a plan that never converges — hence the pairing with
// assignmentRuleToAPI above, and the round-trip test that holds them together.
func assignmentRuleFromAPI(raw map[string]any) *assignmentRuleModel {
	if len(raw) == 0 {
		return nil
	}
	m := &assignmentRuleModel{}
	getStr := func(key string) types.String {
		if v, ok := raw[key].(string); ok && v != "" {
			return types.StringValue(v)
		}
		return types.StringNull()
	}
	getBool := func(key string) types.Bool {
		if v, ok := raw[key].(bool); ok {
			return types.BoolValue(v)
		}
		return types.BoolNull()
	}
	if labels, ok := raw["labels"].(map[string]any); ok && len(labels) > 0 {
		m.Labels = map[string]string{}
		for k, v := range labels {
			if s, ok := v.(string); ok {
				m.Labels[k] = s
			}
		}
	}
	m.NamePrefix = getStr("name_prefix")
	m.NameGlob = getStr("name_glob")
	m.ExecutionBackend = getStr("execution_backend")
	m.ExecutionMode = getStr("execution_mode")
	m.TerraformVersion = getStr("terraform_version")
	m.AgentPoolID = getStr("agent_pool_id")
	m.VCSConnectionID = getStr("vcs_connection_id")
	m.OwnerEmail = getStr("owner_email")
	m.DriftStatus = getStr("drift_status")
	m.Locked = getBool("locked")
	m.HasVCS = getBool("has_vcs")
	return m
}

func buildCreateVarsetRequest(m *variableSetModel) terrapod.CreateVariableSetRequest {
	req := terrapod.CreateVariableSetRequest{
		Name: m.Name.ValueString(),
	}
	if !m.Description.IsNull() {
		req.Description = m.Description.ValueString()
	}
	if !m.Global.IsNull() && !m.Global.IsUnknown() {
		req.Global = m.Global.ValueBool()
	}
	if !m.Priority.IsNull() && !m.Priority.IsUnknown() {
		req.Priority = m.Priority.ValueBool()
	}
	req.AssignmentRule = assignmentRuleToAPI(m.AssignmentRule)
	return req
}

func buildUpdateVarsetRequest(m *variableSetModel) terrapod.UpdateVariableSetRequest {
	req := terrapod.UpdateVariableSetRequest{
		Name: m.Name.ValueString(),
	}
	if !m.Description.IsNull() && !m.Description.IsUnknown() {
		d := m.Description.ValueString()
		req.Description = &d
	}
	if !m.Global.IsNull() && !m.Global.IsUnknown() {
		g := m.Global.ValueBool()
		req.Global = &g
	}
	if !m.Priority.IsNull() && !m.Priority.IsUnknown() {
		p := m.Priority.ValueBool()
		req.Priority = &p
	}
	// Always sent on update, including as nil, so removing the block from the
	// config actually clears the rule rather than leaving it in place.
	rule := assignmentRuleToAPI(m.AssignmentRule)
	req.AssignmentRule = &rule
	return req
}

func readVarsetFromSDK(v *terrapod.VariableSet, m *variableSetModel) {
	m.ID = types.StringValue(v.ID)
	m.Name = types.StringValue(v.Name)
	m.Global = types.BoolValue(v.Global)
	m.Priority = types.BoolValue(v.Priority)
	m.AssignmentRule = assignmentRuleFromAPI(v.AssignmentRule)
	m.VarCount = types.Int64Value(v.VarCount)
	m.WorkspaceCount = types.Int64Value(v.WorkspaceCount)
	m.CreatedAt = types.StringValue(v.CreatedAt)
	m.UpdatedAt = types.StringValue(v.UpdatedAt)
	if v.Description != "" {
		m.Description = types.StringValue(v.Description)
	} else {
		m.Description = types.StringNull()
	}
}
