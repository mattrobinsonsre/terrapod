package inventory_settings

import (
	"context"
	"errors"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
	"github.com/mattrobinsonsre/terrapod/provider/internal/ids"
	"github.com/mattrobinsonsre/terrapod/provider/internal/planmods"
)

var (
	_ resource.Resource                = &inventorySettingsResource{}
	_ resource.ResourceWithImportState = &inventorySettingsResource{}
	_ resource.ResourceWithModifyPlan  = &inventorySettingsResource{}
	_ resource.ResourceWithConfigure   = &inventorySettingsResource{}
)

type inventorySettingsResource struct {
	tc *terrapod.Client
}

// NewResource returns the terrapod_inventory_settings resource.
func NewResource() resource.Resource {
	return &inventorySettingsResource{}
}

func (r *inventorySettingsResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_inventory_settings"
}

func (r *inventorySettingsResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Configures a workspace's ansible inventory: whether the hosts, groups and variables " +
			"declared through Terrapod contribute to the resolution, and the VCS directory merged with them. " +
			"There is one inventory per workspace, so there is one of these per workspace and its id is the " +
			"workspace's. A workspace with no settings resource and no declared rows has no ansible inventory, " +
			"which is the normal state of a terraform/tofu-only workspace. See docs/ansible-inventory.md.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description: "The settings id, which is the workspace id (ws-<uuid>): the inventory is keyed " +
					"on its workspace, so there is no surrogate id to carry.",
				Computed:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"workspace_id": schema.StringAttribute{
				Description: "The workspace whose inventory this configures (e.g. \"ws-abc123\"). It is the " +
					"key, so pointing these settings at another workspace is a destroy and create.",
				Required:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"include_platform": schema.BoolAttribute{
				Description: "Whether the hosts, groups and variables declared through Terrapod contribute to " +
					"the resolution. Defaults to true, which is what a workspace with no settings resource at " +
					"all already does — so adding this resource purely to bind a VCS directory does not " +
					"silently stop the declared rows being used. Set it to false to resolve the VCS source alone.",
				// Computed so the default applies; the default then makes the
				// value known at plan time, so there is nothing for a
				// UseStateForUnknown modifier to do.
				Optional: true,
				Computed: true,
				Default:  booldefault.StaticBool(true),
			},
			"vcs_connection_id": schema.StringAttribute{
				Description: "The VCS connection the inventory directory is read through (e.g. \"vcs-abc123\"). " +
					"Omit it for an inventory declared entirely through Terrapod.",
				Optional: true,
			},
			"repo_url": schema.StringAttribute{
				Description: "The repository holding the inventory directory. Required by the server when " +
					"`vcs_connection_id` is set.",
				Optional: true,
			},
			"branch": schema.StringAttribute{
				Description: "The branch the inventory directory is read from. Empty means the repository's " +
					"default branch.",
				Optional: true,
			},
			"working_directory": schema.StringAttribute{
				Description: "The directory within the repository that holds the inventory. Ansible reads a " +
					"directory as ONE source in lexical filename order, so one binding already carries " +
					"arbitrarily many files and ordering within it is yours to control through filenames. " +
					"Empty means the repository root. This is not the workspace's terraform working directory: " +
					"even in one repository the two differ, and a configure-only workspace has no terraform " +
					"binding at all.",
				Optional: true,
			},
			"ignore_paths": schema.ListAttribute{
				Description: "Paths within the inventory directory that are not read. Useful where the " +
					"directory holds files ansible would try to parse and should not.",
				Optional:    true,
				ElementType: types.StringType,
			},
			"created_at": schema.StringAttribute{
				Description:   "When the settings row was created (RFC3339).",
				Computed:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"updated_at": schema.StringAttribute{
				Description:   "When the settings row last changed (RFC3339).",
				Computed:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
		},
	}
}

func (r *inventorySettingsResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	c, ok := req.ProviderData.(*client.Client)
	if !ok {
		resp.Diagnostics.AddError("Unexpected provider data type",
			fmt.Sprintf("Expected *client.Client, got %T", req.ProviderData))
		return
	}
	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: c.BaseURL, Token: c.Token})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	r.tc = tc
}

func (r *inventorySettingsResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan inventorySettingsModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	put, d := buildPutRequest(ctx, &plan)
	resp.Diagnostics.Append(d...)
	if resp.Diagnostics.HasError() {
		return
	}
	settings, err := r.tc.PutInventorySettings(ctx, plan.WorkspaceID.ValueString(), put)
	if err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}
	resp.Diagnostics.Append(readIntoModel(ctx, settings, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *inventorySettingsResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state inventorySettingsModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	settings, err := r.tc.GetInventorySettings(ctx, state.WorkspaceID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			// No settings row is the normal default state for a workspace, so
			// a 404 means this resource's row is gone rather than that the
			// read failed.
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}
	resp.Diagnostics.Append(readIntoModel(ctx, settings, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *inventorySettingsResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan inventorySettingsModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	put, d := buildPutRequest(ctx, &plan)
	resp.Diagnostics.Append(d...)
	if resp.Diagnostics.HasError() {
		return
	}
	// PUT, not PATCH: a full replace is the right shape when the caller knows
	// the whole intended state, and it is the only one that can CLEAR a field
	// the configuration no longer sets.
	settings, err := r.tc.PutInventorySettings(ctx, plan.WorkspaceID.ValueString(), put)
	if err != nil {
		resp.Diagnostics.AddError("Update failed", err.Error())
		return
	}
	resp.Diagnostics.Append(readIntoModel(ctx, settings, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *inventorySettingsResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state inventorySettingsModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	if err := r.tc.DeleteInventorySettings(ctx, state.WorkspaceID.ValueString()); err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Delete failed", err.Error())
		}
	}
}

// ImportState takes the workspace id, because that is the settings id.
func (r *inventorySettingsResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), req.ID)...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("workspace_id"), req.ID)...)
}

// ModifyPlan keeps a no-change re-plan empty. See
// planmods.KeepComputedWhenUnchanged for why the attribute plan modifiers
// cannot do this alone.
func (r *inventorySettingsResource) ModifyPlan(ctx context.Context, req resource.ModifyPlanRequest, resp *resource.ModifyPlanResponse) {
	planmods.KeepComputedWhenUnchanged(ctx, req, resp,
		[]string{
			"workspace_id", "include_platform", "vcs_connection_id",
			"repo_url", "branch", "working_directory", "ignore_paths",
		},
		[]string{"updated_at"},
	)
}

// buildPutRequest projects the model into the SDK's full-replace shape.
//
// Every field is sent, including the empty ones: PUT replaces, so an omitted
// field would take its default rather than keeping the stored value — which is
// exactly what clearing an attribute in the configuration should do.
func buildPutRequest(ctx context.Context, m *inventorySettingsModel) (terrapod.PutInventorySettingsRequest, diag.Diagnostics) {
	var diags diag.Diagnostics
	req := terrapod.PutInventorySettingsRequest{
		IncludePlatform:  m.IncludePlatform.ValueBool(),
		VCSConnectionID:  m.VCSConnectionID.ValueString(),
		RepoURL:          m.RepoURL.ValueString(),
		Branch:           m.Branch.ValueString(),
		WorkingDirectory: m.WorkingDirectory.ValueString(),
		IgnorePaths:      []string{},
	}
	if !m.IgnorePaths.IsNull() && !m.IgnorePaths.IsUnknown() {
		var paths []string
		diags.Append(m.IgnorePaths.ElementsAs(ctx, &paths, false)...)
		if paths != nil {
			req.IgnorePaths = paths
		}
	}
	return req, diags
}

// readIntoModel projects the server's answer back into the model.
//
// An optional attribute the configuration left out stays null when the server
// holds nothing for it: the server renders an unset string as "" and an unset
// list as absent, and storing those over a null config value fails the apply
// with "Provider produced inconsistent result after apply".
func readIntoModel(ctx context.Context, s *terrapod.InventorySettings, m *inventorySettingsModel) diag.Diagnostics {
	var diags diag.Diagnostics

	m.ID = types.StringValue(s.ID)
	m.IncludePlatform = types.BoolValue(s.IncludePlatform)
	m.CreatedAt = types.StringValue(s.CreatedAt)
	m.UpdatedAt = types.StringValue(s.UpdatedAt)

	// Both id attributes keep the form the configuration wrote whenever it
	// names what the server returned, so neither spelling of a prefixed id
	// drifts into "inconsistent result after apply" (#1748).
	m.WorkspaceID = ids.Keep(m.WorkspaceID, s.WorkspaceID, "ws-")
	m.VCSConnectionID = keepNullableID(m.VCSConnectionID, s.VCSConnectionID, "vcs-")

	m.RepoURL = keepNullWhenEmpty(m.RepoURL, s.RepoURL)
	m.Branch = keepNullWhenEmpty(m.Branch, s.Branch)
	m.WorkingDirectory = keepNullWhenEmpty(m.WorkingDirectory, s.WorkingDirectory)

	if len(s.IgnorePaths) > 0 || !m.IgnorePaths.IsNull() {
		paths := s.IgnorePaths
		if paths == nil {
			paths = []string{}
		}
		v, d := types.ListValueFrom(ctx, types.StringType, paths)
		diags.Append(d...)
		m.IgnorePaths = v
	}

	return diags
}

// keepNullWhenEmpty is the null-preserving read for an optional string: the
// server's "" means "unset", and a configuration that never set it must stay
// null rather than becoming "".
func keepNullWhenEmpty(configured types.String, server string) types.String {
	if server == "" && configured.IsNull() {
		return configured
	}
	return types.StringValue(server)
}

// keepNullableID is keepNullWhenEmpty for an optional id attribute: null when
// there is no relationship, and otherwise the form the configuration wrote
// whenever it names the object the server returned.
func keepNullableID(configured types.String, server, prefix string) types.String {
	if server == "" && configured.IsNull() {
		return configured
	}
	return ids.Keep(configured, server, prefix)
}
