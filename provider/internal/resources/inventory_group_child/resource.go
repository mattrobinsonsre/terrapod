package inventory_group_child

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
	_ resource.Resource                = &inventoryGroupChildResource{}
	_ resource.ResourceWithImportState = &inventoryGroupChildResource{}
	_ resource.ResourceWithConfigure   = &inventoryGroupChildResource{}
)

type inventoryGroupChildResource struct {
	tc *terrapod.Client
}

// NewResource returns the terrapod_inventory_group_child resource.
func NewResource() resource.Resource {
	return &inventoryGroupChildResource{}
}

func (r *inventoryGroupChildResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_inventory_group_child"
}

func (r *inventoryGroupChildResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Nests one group inside another — a `[groupname:children]` entry in an ansible " +
			"inventory. A group may have several parents, and the server refuses a cycle. There is nothing to " +
			"change about a nesting that is not a different nesting, so both sides force a replacement. " +
			"Nesting carries the structure a resolved inventory's per-group host list does not: ansible does " +
			"not flatten it, so a parent whose members all arrive through a child reports none of its own. " +
			"See docs/ansible-inventory.md.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description:   "The nesting id (invgc-<uuid>).",
				Computed:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"parent_group_id": schema.StringAttribute{
				Description:   "The group that gains a child (e.g. \"invgroup-abc123\").",
				Required:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"child_group_id": schema.StringAttribute{
				Description:   "The group nested inside the parent (e.g. \"invgroup-def456\").",
				Required:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"created_at": schema.StringAttribute{
				Description:   "When the nesting was declared (RFC3339).",
				Computed:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
		},
	}
}

func (r *inventoryGroupChildResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *inventoryGroupChildResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan inventoryGroupChildModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	// The parent's side, always. The SDK's child-side creator writes the same
	// row, and picking one keeps this resource to a single code path.
	gc, err := r.tc.AddInventoryGroupChild(ctx,
		plan.ParentGroupID.ValueString(), plan.ChildGroupID.ValueString())
	if err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}
	readIntoModel(gc, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *inventoryGroupChildResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state inventoryGroupChildModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	gc, err := r.tc.GetInventoryGroupChild(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			// Deleting either group cascades the nesting, so a 404 here is the
			// ordinary way this row disappears.
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}
	readIntoModel(gc, &state)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

// Update is unreachable: both attributes a configuration can write force a
// replacement, so the framework never asks for one. It refuses rather than
// silently doing nothing, so that removing a RequiresReplace by accident shows
// up as an error instead of as a successful no-op.
func (r *inventoryGroupChildResource) Update(_ context.Context, _ resource.UpdateRequest, resp *resource.UpdateResponse) {
	resp.Diagnostics.AddError("Update not supported",
		"A group nesting is immutable — there is nothing to change about it that is not a different "+
			"nesting. Delete and recreate instead.")
}

func (r *inventoryGroupChildResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state inventoryGroupChildModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	// Both groups remain; only the nesting goes.
	if err := r.tc.DeleteInventoryGroupChild(ctx, state.ID.ValueString()); err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Delete failed", err.Error())
		}
	}
}

// ImportState takes the nesting id. Both sides come back from the read,
// because the row is addressable in its own right.
func (r *inventoryGroupChildResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

// readIntoModel projects the server's answer into the model.
//
// There is no updated_at, so there is no ModifyPlan: a nesting never changes,
// and the only computed attributes are the id and created_at, which
// UseStateForUnknown already holds across a no-change re-plan.
func readIntoModel(gc *terrapod.InventoryGroupChild, m *inventoryGroupChildModel) {
	m.ID = types.StringValue(gc.ID)
	m.CreatedAt = types.StringValue(gc.CreatedAt)
	// Both sides keep the form the configuration wrote whenever it names what
	// the server returned (#1748) — and these attributes force a replacement,
	// so a drifted spelling would destroy and recreate the nesting.
	m.ParentGroupID = ids.Keep(m.ParentGroupID, gc.ParentGroupID, "invgroup-")
	m.ChildGroupID = ids.Keep(m.ChildGroupID, gc.ChildGroupID, "invgroup-")
}
