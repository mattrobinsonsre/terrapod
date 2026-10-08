package inventory_host_var

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
	"github.com/mattrobinsonsre/terrapod/provider/internal/ids"
	"github.com/mattrobinsonsre/terrapod/provider/internal/planmods"
)

var (
	_ resource.Resource                = &inventoryHostVarResource{}
	_ resource.ResourceWithImportState = &inventoryHostVarResource{}
	_ resource.ResourceWithModifyPlan  = &inventoryHostVarResource{}
	_ resource.ResourceWithConfigure   = &inventoryHostVarResource{}
)

type inventoryHostVarResource struct {
	tc *terrapod.Client
}

// NewResource returns the terrapod_inventory_host_var resource.
func NewResource() resource.Resource {
	return &inventoryHostVarResource{}
}

func (r *inventoryHostVarResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_inventory_host_var"
}

func (r *inventoryHostVarResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Declares one entry in a host's ansible `host_vars` — `ansible_host`, `ansible_user`, " +
			"or anything a role reads. A variable is its own resource rather than a map entry on the host so " +
			"that it has exactly one writer: a second concern can add a host variable without taking " +
			"ownership of every other variable that host has. See docs/ansible-inventory.md.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description:   "The variable id (invhvar-<uuid>).",
				Computed:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"host_id": schema.StringAttribute{
				Description: "The host this variable belongs to (e.g. \"invhost-abc123\"). A variable belongs " +
					"to one host, so moving it is a destroy and create.",
				Required:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"key": schema.StringAttribute{
				Description: "The variable name, unique within the host. Renaming is done in place, so this " +
					"does not force a replacement; a name the host already holds is refused.",
				Required: true,
			},
			"value": schema.StringAttribute{
				Description: "The value. Text unless `structured` is set, in which case it is literal source " +
					"for a list, number, bool or object. Redacted in plan output unconditionally, because " +
					"Terraform cannot vary an attribute's sensitivity per row — `sensitive` controls what the " +
					"API returns, not what the plan prints. The value is held in Terraform state either way.",
				Required:  true,
				Sensitive: true,
			},
			"structured": schema.BoolAttribute{
				Description: "Whether `value` is a typed expression rather than a plain string. Set it to " +
					"express a list, number, bool or object by sending its source.",
				Optional: true,
				Computed: true,
				Default:  booldefault.StaticBool(false),
			},
			"sensitive": schema.BoolAttribute{
				Description: "Whether the API masks the value in every response. A display flag, not " +
					"encryption: every inventory variable value is encrypted at rest regardless, because a " +
					"column cannot be conditionally encrypted and a host variable is an ordinary place for a " +
					"become password. A sensitive variable therefore never reads back — Terraform keeps what " +
					"the configuration says, and an IMPORTED one takes the mask until the next apply sets it.",
				Optional: true,
				Computed: true,
				Default:  booldefault.StaticBool(false),
			},
			"created_at": schema.StringAttribute{
				Description:   "When the variable was declared (RFC3339).",
				Computed:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"updated_at": schema.StringAttribute{
				Description:   "When the variable last changed (RFC3339).",
				Computed:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
		},
	}
}

func (r *inventoryHostVarResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *inventoryHostVarResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan inventoryHostVarModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	v, err := r.tc.CreateInventoryHostVar(ctx, plan.HostID.ValueString(), buildCreateRequest(&plan))
	if err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}
	readIntoModel(v, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *inventoryHostVarResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state inventoryHostVarModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	v, err := r.tc.GetInventoryHostVar(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			// Deleting the host cascades its variables, so a 404 here is the
			// ordinary way this row disappears.
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}
	readIntoModel(v, &state)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *inventoryHostVarResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan inventoryHostVarModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	v, err := r.tc.UpdateInventoryHostVar(ctx, plan.ID.ValueString(), buildUpdateRequest(&plan))
	if err != nil {
		resp.Diagnostics.AddError("Update failed", err.Error())
		return
	}
	readIntoModel(v, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *inventoryHostVarResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state inventoryHostVarModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	if err := r.tc.DeleteInventoryHostVar(ctx, state.ID.ValueString()); err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Delete failed", err.Error())
		}
	}
}

// ImportState takes the variable id. The host comes back from the read.
func (r *inventoryHostVarResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

// ModifyPlan keeps a no-change re-plan empty. See
// planmods.KeepComputedWhenUnchanged for why the attribute plan modifiers
// cannot do this alone.
func (r *inventoryHostVarResource) ModifyPlan(ctx context.Context, req resource.ModifyPlanRequest, resp *resource.ModifyPlanResponse) {
	planmods.KeepComputedWhenUnchanged(ctx, req, resp,
		[]string{"host_id", "key", "value", "structured", "sensitive"},
		[]string{"updated_at"},
	)
}

func buildCreateRequest(m *inventoryHostVarModel) terrapod.CreateInventoryVarRequest {
	return terrapod.CreateInventoryVarRequest{
		Key:        m.Key.ValueString(),
		Value:      m.Value.ValueString(),
		Structured: m.Structured.ValueBool(),
		Sensitive:  m.Sensitive.ValueBool(),
	}
}

// buildUpdateRequest sends every field.
//
// The SDK's pointers draw "leave alone" apart from "set", which a caller
// holding only a partial intent needs. Terraform always knows the whole
// intended state, so there is nothing to leave alone — and sending the whole
// row is what lets a `sensitive` flip back to false also restore the value the
// configuration holds, which a partial update could not.
func buildUpdateRequest(m *inventoryHostVarModel) terrapod.UpdateInventoryVarRequest {
	key := m.Key.ValueString()
	value := m.Value.ValueString()
	structured := m.Structured.ValueBool()
	sensitive := m.Sensitive.ValueBool()
	return terrapod.UpdateInventoryVarRequest{
		Key:        &key,
		Value:      &value,
		Structured: &structured,
		Sensitive:  &sensitive,
	}
}

// readIntoModel projects the server's answer into the model.
func readIntoModel(v *terrapod.InventoryVar, m *inventoryHostVarModel) {
	m.ID = types.StringValue(v.ID)
	m.Key = types.StringValue(v.Key)
	m.Structured = types.BoolValue(v.Structured)
	m.Sensitive = types.BoolValue(v.Sensitive)
	m.CreatedAt = types.StringValue(v.CreatedAt)
	m.UpdatedAt = types.StringValue(v.UpdatedAt)
	// The configuration's own spelling of the host id is kept whenever it
	// names the host the server returned (#1748) — and this attribute forces a
	// replacement, so a drifted spelling would destroy and recreate the row.
	m.HostID = ids.Keep(m.HostID, v.HostID, "invhost-")
	m.Value = keepUnreadableValue(m.Value, v.Value)
}

// keepUnreadableValue is the read rule for a value the server will not return.
//
// A sensitive variable reads back as terrapod.MaskedValue, so taking the
// server's answer would store the mask as the secret — and then every
// subsequent plan would show an update from "***" to the configured value that
// never converges. The configuration is the source of truth for a value we
// cannot read back, so state keeps what it already has.
//
// An import has no prior value, so it takes the mask: that is the honest
// answer, and the next plan correctly shows the configured value replacing it.
func keepUnreadableValue(configured types.String, server string) types.String {
	if server == terrapod.MaskedValue &&
		!configured.IsNull() && !configured.IsUnknown() &&
		configured.ValueString() != terrapod.MaskedValue {
		return configured
	}
	return types.StringValue(server)
}
