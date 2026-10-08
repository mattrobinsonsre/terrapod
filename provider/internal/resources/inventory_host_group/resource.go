package inventory_host_group

import (
	"context"
	"errors"
	"fmt"

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
	_ resource.Resource                = &inventoryHostGroupResource{}
	_ resource.ResourceWithImportState = &inventoryHostGroupResource{}
	_ resource.ResourceWithConfigure   = &inventoryHostGroupResource{}
)

type inventoryHostGroupResource struct {
	tc *terrapod.Client
}

// NewResource returns the terrapod_inventory_host_group resource.
func NewResource() resource.Resource {
	return &inventoryHostGroupResource{}
}

func (r *inventoryHostGroupResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_inventory_host_group"
}

func (r *inventoryHostGroupResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Puts one host in one group — a `[groupname]` host line in an ansible inventory. " +
			"Many-to-many, and its own resource so that a second concern can place a host in a group without " +
			"owning either side. There is nothing to change about a membership that is not a different " +
			"membership, so both sides force a replacement. See docs/ansible-inventory.md.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description:   "The membership id (invhg-<uuid>).",
				Computed:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"host_id": schema.StringAttribute{
				Description:   "The host joining the group (e.g. \"invhost-abc123\").",
				Required:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"group_id": schema.StringAttribute{
				Description:   "The group the host joins (e.g. \"invgroup-abc123\").",
				Required:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"created_at": schema.StringAttribute{
				Description:   "When the membership was declared (RFC3339).",
				Computed:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
		},
	}
}

func (r *inventoryHostGroupResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *inventoryHostGroupResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan inventoryHostGroupModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	// The group's side, always. The SDK's host-side creator writes the same
	// row, and picking one keeps this resource to a single code path.
	hg, err := r.tc.AddHostToInventoryGroup(ctx,
		plan.GroupID.ValueString(), plan.HostID.ValueString())
	if err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}
	readIntoModel(hg, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *inventoryHostGroupResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state inventoryHostGroupModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	hg, err := r.tc.GetInventoryHostGroup(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			// Deleting either the host or the group cascades the membership,
			// so a 404 here is the ordinary way this row disappears.
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}
	readIntoModel(hg, &state)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

// Update is unreachable: both attributes a configuration can write force a
// replacement, so the framework never asks for one. It refuses rather than
// silently doing nothing, so that removing a RequiresReplace by accident shows
// up as an error instead of as a successful no-op.
func (r *inventoryHostGroupResource) Update(_ context.Context, _ resource.UpdateRequest, resp *resource.UpdateResponse) {
	resp.Diagnostics.AddError("Update not supported",
		"A host's membership of a group is immutable — there is nothing to change about it that is not a "+
			"different membership. Delete and recreate instead.")
}

func (r *inventoryHostGroupResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state inventoryHostGroupModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	// The host and the group remain; only the membership goes.
	if err := r.tc.DeleteInventoryHostGroup(ctx, state.ID.ValueString()); err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Delete failed", err.Error())
		}
	}
}

// ImportState takes the membership id. Both sides come back from the read,
// because the row is addressable in its own right.
func (r *inventoryHostGroupResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

// readIntoModel projects the server's answer into the model.
//
// There is no updated_at, so there is no ModifyPlan: a membership never
// changes, and the only computed attributes are the id and created_at, which
// UseStateForUnknown already holds across a no-change re-plan.
func readIntoModel(hg *terrapod.InventoryHostGroup, m *inventoryHostGroupModel) {
	m.ID = types.StringValue(hg.ID)
	m.CreatedAt = types.StringValue(hg.CreatedAt)
	// Both sides keep the form the configuration wrote whenever it names what
	// the server returned, so interpolating either spelling of a prefixed id
	// does not fail the apply as "inconsistent result after apply" (#1748) —
	// and these attributes force a replacement, so a drifted spelling would
	// destroy and recreate the membership.
	m.HostID = ids.Keep(m.HostID, hg.HostID, "invhost-")
	m.GroupID = ids.Keep(m.GroupID, hg.GroupID, "invgroup-")
}
