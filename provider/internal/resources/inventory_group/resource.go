package inventory_group

import (
	"context"
	"errors"
	"fmt"
	"regexp"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/schema/validator"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
	"github.com/mattrobinsonsre/terrapod/provider/internal/ids"
	"github.com/mattrobinsonsre/terrapod/provider/internal/planmods"
)

var (
	_ resource.Resource                = &inventoryGroupResource{}
	_ resource.ResourceWithImportState = &inventoryGroupResource{}
	_ resource.ResourceWithModifyPlan  = &inventoryGroupResource{}
	_ resource.ResourceWithConfigure   = &inventoryGroupResource{}
)

// groupNamePattern is what ansible can reach as `{{ groupname }}` and use in a
// group_vars filename without warning.
var groupNamePattern = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

// derivedGroups are ansible's to produce, not ours to declare: `all` holds
// every host and is the rendered document's root key, and `ungrouped` holds the
// hosts in no other group, so a declaration cannot control either one's
// membership.
var derivedGroups = map[string]bool{"all": true, "ungrouped": true}

type inventoryGroupResource struct {
	tc *terrapod.Client
}

// NewResource returns the terrapod_inventory_group resource.
func NewResource() resource.Resource {
	return &inventoryGroupResource{}
}

func (r *inventoryGroupResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_inventory_group"
}

func (r *inventoryGroupResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Declares one group in a Terrapod workspace's ansible inventory. A group is a name: its " +
			"members are `terrapod_inventory_host_group` rows, its nesting is " +
			"`terrapod_inventory_group_child` rows and its variables are `terrapod_inventory_group_var` rows — " +
			"each its own resource, so a second concern can contribute to a group it does not own. `all` and " +
			"`ungrouped` cannot be declared: ansible derives both. See docs/ansible-inventory.md.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description:   "The group id (invgroup-<uuid>).",
				Computed:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"workspace_id": schema.StringAttribute{
				Description: "The workspace whose inventory declares this group (e.g. \"ws-abc123\"). A group " +
					"belongs to one workspace, so moving it is a destroy and create.",
				Required:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"name": schema.StringAttribute{
				Description: "The group name, unique within the workspace. Renaming is done in place, so this " +
					"does not force a replacement — the memberships and nestings that point at the group are " +
					"rows of their own and survive it. `all` and `ungrouped` are refused because ansible " +
					"derives them.",
				Required:   true,
				Validators: []validator.String{groupNameValidator{}},
			},
			"created_at": schema.StringAttribute{
				Description:   "When the group was declared (RFC3339).",
				Computed:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"updated_at": schema.StringAttribute{
				Description:   "When the group last changed (RFC3339).",
				Computed:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
		},
	}
}

func (r *inventoryGroupResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *inventoryGroupResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan inventoryGroupModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	group, err := r.tc.CreateInventoryGroup(ctx,
		plan.WorkspaceID.ValueString(), plan.Name.ValueString())
	if err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}
	readIntoModel(group, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *inventoryGroupResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state inventoryGroupModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	group, err := r.tc.GetInventoryGroup(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}
	readIntoModel(group, &state)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *inventoryGroupResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan inventoryGroupModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	group, err := r.tc.UpdateInventoryGroup(ctx, plan.ID.ValueString(), plan.Name.ValueString())
	if err != nil {
		resp.Diagnostics.AddError("Update failed", err.Error())
		return
	}
	readIntoModel(group, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *inventoryGroupResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state inventoryGroupModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	// Deleting a group cascades its variables, its memberships and its
	// nestings in both directions server-side. The hosts remain.
	if err := r.tc.DeleteInventoryGroup(ctx, state.ID.ValueString()); err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Delete failed", err.Error())
		}
	}
}

// ImportState takes the group id. The workspace comes back from the read.
func (r *inventoryGroupResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

// ModifyPlan keeps a no-change re-plan empty. See
// planmods.KeepComputedWhenUnchanged for why the attribute plan modifiers
// cannot do this alone.
func (r *inventoryGroupResource) ModifyPlan(ctx context.Context, req resource.ModifyPlanRequest, resp *resource.ModifyPlanResponse) {
	planmods.KeepComputedWhenUnchanged(ctx, req, resp,
		[]string{"workspace_id", "name"},
		[]string{"updated_at"},
	)
}

// readIntoModel projects the server's answer into the model.
func readIntoModel(g *terrapod.InventoryGroup, m *inventoryGroupModel) {
	m.ID = types.StringValue(g.ID)
	m.Name = types.StringValue(g.Name)
	m.CreatedAt = types.StringValue(g.CreatedAt)
	m.UpdatedAt = types.StringValue(g.UpdatedAt)
	// The configuration's own spelling of the workspace id is kept whenever it
	// names the workspace the server returned (#1748).
	m.WorkspaceID = ids.Keep(m.WorkspaceID, g.WorkspaceID, "ws-")
}

type groupNameValidator struct{}

var _ validator.String = groupNameValidator{}

func (groupNameValidator) Description(_ context.Context) string {
	return "a group name must be an identifier, and never `all` or `ungrouped`"
}

func (v groupNameValidator) MarkdownDescription(ctx context.Context) string {
	return v.Description(ctx)
}

func (groupNameValidator) ValidateString(_ context.Context, req validator.StringRequest, resp *validator.StringResponse) {
	if req.ConfigValue.IsNull() || req.ConfigValue.IsUnknown() {
		return
	}
	if msg := groupNameProblem(req.ConfigValue.ValueString()); msg != "" {
		resp.Diagnostics.AddAttributeError(req.Path, "Invalid inventory group name", msg)
	}
}

// groupNameProblem returns why value cannot be an ansible group name, or "".
func groupNameProblem(value string) string {
	if value == "" {
		return "a group name must not be empty"
	}
	if derivedGroups[value] {
		return fmt.Sprintf("%q is derived by ansible, not declared: 'all' holds every host and is the "+
			"rendered document's root key, and 'ungrouped' holds the hosts in no other group, so a "+
			"declaration cannot control either one's membership. Name a group of your own instead, and "+
			"put variables that apply to everything in a terrapod_inventory_global_var.", value)
	}
	if !groupNamePattern.MatchString(value) {
		return fmt.Sprintf("group name %q must start with a letter or underscore and contain only "+
			"letters, digits and underscores. Ansible tolerates more than that but warns, and a name it "+
			"warns about cannot be used reliably in a --limit pattern or a group_vars filename.", value)
	}
	return ""
}
