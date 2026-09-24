// Package policy_set implements the terrapod_policy_set resource (#1765).
//
// API Contract (Terrapod API ↔ Terraform Provider):
//
//	JSON:API type: "policy-sets"
//	Create:  POST   /api/terrapod/v1/policy-sets
//	Read:    GET    /api/terrapod/v1/policy-sets/{id}
//	Update:  PATCH  /api/terrapod/v1/policy-sets/{id}
//	Delete:  DELETE /api/terrapod/v1/policy-sets/{id}
//
// A policy set can block applies across the whole estate, and until this
// existed it was the one gate that could only be click-opped: the model and a
// full CRUD API were both there, but no resource, so the thing governing the
// infrastructure was the one thing not managed as code.
//
// Scope is modelled exactly as `terrapod_role` models it, because the server
// matches it with the same code — `policy_set_service._labels_match`, whose
// docstring says so. One key binds one accepted value, `{env = "prod"}`, and a
// workspace matching any one entry is in scope.
//
// The set's POLICIES are not attributes here. They are separate rows with
// their own endpoints, and the repo already has a shape for parent-and-children
// (`terrapod_variable_set` / `terrapod_variable_set_variable`); a
// `terrapod_policy` resource follows it. An inline set with no policies is
// valid and evaluates as passing — it is the container, not the rule.
package policy_set

import (
	"context"
	"errors"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
	"github.com/mattrobinsonsre/terrapod/provider/internal/ids"
)

var (
	_ resource.Resource                = &policySetResource{}
	_ resource.ResourceWithImportState = &policySetResource{}
)

type policySetModel struct {
	ID types.String `tfsdk:"id"`

	Name             types.String `tfsdk:"name"`
	Description      types.String `tfsdk:"description"`
	EnforcementLevel types.String `tfsdk:"enforcement_level"`
	Enabled          types.Bool   `tfsdk:"enabled"`

	GlobalScope types.Bool `tfsdk:"global_scope"`
	AllowLabels types.Map  `tfsdk:"allow_labels"`
	AllowNames  types.List `tfsdk:"allow_names"`
	DenyLabels  types.Map  `tfsdk:"deny_labels"`
	DenyNames   types.List `tfsdk:"deny_names"`

	Source           types.String `tfsdk:"source"`
	VCSConnectionID  types.String `tfsdk:"vcs_connection_id"`
	VCSRepoURL       types.String `tfsdk:"vcs_repo_url"`
	VCSBranch        types.String `tfsdk:"vcs_branch"`
	PolicyPath       types.String `tfsdk:"policy_path"`
	VCSLastCommitSHA types.String `tfsdk:"vcs_last_commit_sha"`
	VCSLastSyncedAt  types.String `tfsdk:"vcs_last_synced_at"`
	VCSLastError     types.String `tfsdk:"vcs_last_error"`

	PolicyCount types.Int64  `tfsdk:"policy_count"`
	CreatedAt   types.String `tfsdk:"created_at"`
	UpdatedAt   types.String `tfsdk:"updated_at"`
}

type policySetResource struct {
	client *client.Client
	tc     *terrapod.Client
}

func NewResource() resource.Resource {
	return &policySetResource{}
}

func (r *policySetResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_policy_set"
}

func (r *policySetResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages an OPA policy set — a scoped collection of Rego policies evaluated against a run's plan.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description: "The policy set's id.",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"name": schema.StringAttribute{
				Description: "The policy set's name.",
				Required:    true,
			},
			"description": schema.StringAttribute{
				Description: "Human-readable description of what the set is for.",
				Optional:    true,
			},
			"enforcement_level": schema.StringAttribute{
				Description: "`advisory` records a warning and never blocks; `mandatory` blocks the apply when a policy denies, until an admin overrides. Defaults to `advisory`.",
				Optional:    true,
				Computed:    true,
			},
			"enabled": schema.BoolAttribute{
				Description: "Whether the set is evaluated at all. A disabled set applies to nothing, whatever its scope. Defaults to true.",
				Optional:    true,
				Computed:    true,
			},

			// ── Scope ────────────────────────────────────────────────────
			"global_scope": schema.BoolAttribute{
				Description: "Apply to EVERY workspace, ignoring the allow rules. Deny rules still take precedence, so \"everywhere except these\" stays expressible. Defaults to false.",
				Optional:    true,
				Computed:    true,
			},
			// One key binds one value, matching terrapod_role on this line:
			// { env = "prod" }. The server matches these with the same code
			// that matches a role's, so the two resources agree on the shape.
			"allow_labels": schema.MapAttribute{
				Description: "Workspace labels this set applies to. A workspace matching any one entry is in scope.",
				Optional:    true,
				ElementType: types.StringType,
			},
			"allow_names": schema.ListAttribute{
				Description: "Workspace names this set applies to.",
				Optional:    true,
				ElementType: types.StringType,
			},
			"deny_labels": schema.MapAttribute{
				Description: "Workspace labels this set never applies to. Deny wins over allow, and over `global_scope`.",
				Optional:    true,
				ElementType: types.StringType,
			},
			"deny_names": schema.ListAttribute{
				Description: "Workspace names this set never applies to. Deny wins over allow.",
				Optional:    true,
				ElementType: types.StringType,
			},

			// ── Where the policies come from ─────────────────────────────
			"source": schema.StringAttribute{
				Description: "`inline` (policies are managed as `terrapod_policy` resources) or `vcs` (policies are synced from a repository). Changing this forces a new resource. Defaults to `inline`.",
				Optional:    true,
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"vcs_connection_id": schema.StringAttribute{
				Description: "The VCS connection to sync from. Required when `source` is `vcs`. Changing it forces a new resource, because the server does not accept a connection change on update.",
				Optional:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"vcs_repo_url": schema.StringAttribute{
				Description: "Repository holding the policies, when `source` is `vcs`.",
				Optional:    true,
			},
			"vcs_branch": schema.StringAttribute{
				Description: "Branch to sync policies from. Empty means the repository's default branch.",
				Optional:    true,
			},
			"policy_path": schema.StringAttribute{
				Description: "Subdirectory within the repository holding the `.rego` files. Empty means the repository root.",
				Optional:    true,
			},

			// ── Read-only ────────────────────────────────────────────────
			"vcs_last_commit_sha": schema.StringAttribute{
				Description: "The commit the policies were last synced from.",
				Computed:    true,
			},
			"vcs_last_synced_at": schema.StringAttribute{
				Description: "When the last sync completed.",
				Computed:    true,
			},
			"vcs_last_error": schema.StringAttribute{
				Description: "Why the last sync failed, or empty. Worth alerting on: a set that cannot sync is evaluated against whatever it last managed to fetch.",
				Computed:    true,
			},
			"policy_count": schema.Int64Attribute{
				Description: "How many policies the set currently holds.",
				Computed:    true,
			},
			"created_at": schema.StringAttribute{
				Description: "When the set was created (RFC3339).",
				Computed:    true,
			},
			"updated_at": schema.StringAttribute{
				Description: "When the set was last updated (RFC3339).",
				Computed:    true,
			},
		},
	}
}

func (r *policySetResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	c, ok := req.ProviderData.(*client.Client)
	if !ok {
		resp.Diagnostics.AddError(
			"Unexpected provider data type",
			fmt.Sprintf("Expected *client.Client, got %T", req.ProviderData),
		)
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

func (r *policySetResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var m policySetModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}

	ps, err := r.tc.CreatePolicySet(ctx, buildCreateRequest(&m))
	if err != nil {
		resp.Diagnostics.AddError("Unable to create policy set", err.Error())
		return
	}

	resp.Diagnostics.Append(readFromSDK(ctx, ps, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *policySetResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var m policySetModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}

	ps, err := r.tc.GetPolicySet(ctx, m.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			// Deleted outside Terraform: drop it from state rather than error,
			// so the next plan recreates it.
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Unable to read policy set", err.Error())
		return
	}

	resp.Diagnostics.Append(readFromSDK(ctx, ps, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *policySetResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var m policySetModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}

	ps, err := r.tc.UpdatePolicySet(ctx, m.ID.ValueString(), buildUpdateRequest(&m))
	if err != nil {
		resp.Diagnostics.AddError("Unable to update policy set", err.Error())
		return
	}

	resp.Diagnostics.Append(readFromSDK(ctx, ps, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *policySetResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var m policySetModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}

	if err := r.tc.DeletePolicySet(ctx, m.ID.ValueString()); err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			// Already gone; deleting is what we wanted.
			return
		}
		resp.Diagnostics.AddError("Unable to delete policy set", err.Error())
	}
}

func (r *policySetResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

func buildCreateRequest(m *policySetModel) terrapod.CreatePolicySetRequest {
	return terrapod.CreatePolicySetRequest{
		Name:             m.Name.ValueString(),
		Description:      m.Description.ValueString(),
		EnforcementLevel: m.EnforcementLevel.ValueString(),
		Enabled:          m.Enabled.IsNull() || m.Enabled.ValueBool(),
		GlobalScope:      m.GlobalScope.ValueBool(),
		AllowLabels:      mapFromTFMap(m.AllowLabels),
		AllowNames:       sliceFromTFList(m.AllowNames),
		DenyLabels:       mapFromTFMap(m.DenyLabels),
		DenyNames:        sliceFromTFList(m.DenyNames),
		Source:           m.Source.ValueString(),
		VCSConnectionID:  m.VCSConnectionID.ValueString(),
		VCSRepoURL:       m.VCSRepoURL.ValueString(),
		VCSBranch:        m.VCSBranch.ValueString(),
		PolicyPath:       m.PolicyPath.ValueString(),
	}
}

func buildUpdateRequest(m *policySetModel) terrapod.UpdatePolicySetRequest {
	// A PATCH sends only what the configuration states. The scope collections
	// are sent as "or empty" so that REMOVING every allow label is expressed
	// as an empty map rather than as "leave it alone" — a scope that cannot be
	// narrowed by deleting a line is a scope Terraform does not really own.
	name := m.Name.ValueString()
	desc := m.Description.ValueString()
	enabled := m.Enabled.ValueBool()
	global := m.GlobalScope.ValueBool()

	req := terrapod.UpdatePolicySetRequest{
		Name:        &name,
		Description: &desc,
		Enabled:     &enabled,
		GlobalScope: &global,
		AllowLabels: mapFromTFMapOrEmpty(m.AllowLabels),
		AllowNames:  sliceFromTFListOrEmpty(m.AllowNames),
		DenyLabels:  mapFromTFMapOrEmpty(m.DenyLabels),
		DenyNames:   sliceFromTFListOrEmpty(m.DenyNames),
	}
	if !m.EnforcementLevel.IsNull() && !m.EnforcementLevel.IsUnknown() {
		level := m.EnforcementLevel.ValueString()
		req.EnforcementLevel = &level
	}
	if !m.VCSRepoURL.IsNull() {
		v := m.VCSRepoURL.ValueString()
		req.VCSRepoURL = &v
	}
	if !m.VCSBranch.IsNull() {
		v := m.VCSBranch.ValueString()
		req.VCSBranch = &v
	}
	if !m.PolicyPath.IsNull() {
		v := m.PolicyPath.ValueString()
		req.PolicyPath = &v
	}
	return req
}

func readFromSDK(ctx context.Context, ps *terrapod.PolicySet, m *policySetModel) diag.Diagnostics {
	var diags diag.Diagnostics

	m.ID = types.StringValue(ps.ID)
	m.Name = types.StringValue(ps.Name)
	m.EnforcementLevel = types.StringValue(ps.EnforcementLevel)
	m.Enabled = types.BoolValue(ps.Enabled)
	m.GlobalScope = types.BoolValue(ps.GlobalScope)
	m.Source = types.StringValue(ps.Source)
	m.PolicyCount = types.Int64Value(ps.PolicyCount)
	m.VCSLastCommitSHA = types.StringValue(ps.VCSLastCommitSHA)
	m.VCSLastSyncedAt = types.StringValue(ps.VCSLastSyncedAt)
	m.VCSLastError = types.StringValue(ps.VCSLastError)
	m.CreatedAt = types.StringValue(ps.CreatedAt)
	m.UpdatedAt = types.StringValue(ps.UpdatedAt)

	// The endpoint serialises the connection id bare while a data source's
	// `.id` is prefixed, and a configuration naturally interpolates the latter.
	// Keep whichever form was written (#1748).
	m.VCSConnectionID = ids.Keep(m.VCSConnectionID, ps.VCSConnectionID, "vcs-")

	// Optional strings: an empty answer stays null when the configuration said
	// nothing, so an unset attribute does not read back as "" and plan forever.
	m.Description = optionalString(m.Description, ps.Description)
	m.VCSRepoURL = optionalString(m.VCSRepoURL, ps.VCSRepoURL)
	m.VCSBranch = optionalString(m.VCSBranch, ps.VCSBranch)
	m.PolicyPath = optionalString(m.PolicyPath, ps.PolicyPath)

	m.AllowLabels, diags = labelsToTF(ctx, m.AllowLabels, ps.AllowLabels)
	if diags.HasError() {
		return diags
	}
	m.DenyLabels, diags = labelsToTF(ctx, m.DenyLabels, ps.DenyLabels)
	if diags.HasError() {
		return diags
	}

	m.AllowNames, diags = namesToTF(ctx, m.AllowNames, ps.AllowNames)
	if diags.HasError() {
		return diags
	}
	m.DenyNames, diags = namesToTF(ctx, m.DenyNames, ps.DenyNames)
	if diags.HasError() {
		return diags
	}
	return diags
}

// namesToTF mirrors labelsToTF for the name rules. Reading them back is what
// makes a change made elsewhere show as a diff — and, more importantly, what
// stops an imported set losing its scoping: `buildUpdateRequest` sends a
// non-nil empty slice for a null list, so without this the first apply after
// an import PATCHed `allow-names: []` and erased the rule.
func namesToTF(
	ctx context.Context, configured types.List, server []string,
) (types.List, diag.Diagnostics) {
	if len(server) == 0 && (configured.IsNull() || configured.IsUnknown()) {
		return types.ListNull(types.StringType), nil
	}
	return types.ListValueFrom(ctx, types.StringType, server)
}

// optionalString keeps an unset attribute null rather than "" — the server
// answers an absent optional with an empty string, and storing that would make
// every subsequent plan show a change from null to "".
func optionalString(configured types.String, server string) types.String {
	if server == "" && (configured.IsNull() || configured.IsUnknown()) {
		return types.StringNull()
	}
	return types.StringValue(server)
}

// labelsToTF converts the server's label rule, leaving an unset attribute null
// rather than an empty map for the same reason as optionalString.
//
// One key binds one value, matching terrapod_role on this line — the server
// scores both with the same matcher, so the two resources must not disagree
// about the shape a rule takes.
func labelsToTF(
	ctx context.Context, configured types.Map, server map[string]string,
) (types.Map, diag.Diagnostics) {
	if len(server) == 0 && (configured.IsNull() || configured.IsUnknown()) {
		return types.MapNull(types.StringType), nil
	}
	return types.MapValueFrom(ctx, types.StringType, server)
}

func mapFromTFMap(m types.Map) map[string]string {
	if m.IsNull() || m.IsUnknown() {
		return nil
	}
	return mapFromTFMapOrEmpty(m)
}

func mapFromTFMapOrEmpty(m types.Map) map[string]string {
	out := map[string]string{}
	if m.IsNull() || m.IsUnknown() {
		return out
	}
	for k, v := range m.Elements() {
		if s, ok := v.(types.String); ok {
			out[k] = s.ValueString()
		}
	}
	return out
}

func sliceFromTFList(l types.List) []string {
	if l.IsNull() || l.IsUnknown() {
		return nil
	}
	return sliceFromTFListOrEmpty(l)
}

func sliceFromTFListOrEmpty(l types.List) []string {
	out := []string{}
	if l.IsNull() || l.IsUnknown() {
		return out
	}
	for _, e := range l.Elements() {
		if s, ok := e.(types.String); ok {
			out = append(out, s.ValueString())
		}
	}
	return out
}
