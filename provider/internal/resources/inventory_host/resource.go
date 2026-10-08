package inventory_host

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"
	"unicode"

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
	_ resource.Resource                = &inventoryHostResource{}
	_ resource.ResourceWithImportState = &inventoryHostResource{}
	_ resource.ResourceWithModifyPlan  = &inventoryHostResource{}
	_ resource.ResourceWithConfigure   = &inventoryHostResource{}
)

// forbiddenHostNameChars are --limit's own separators and operators: ',' and
// ':' separate patterns, '!' excludes, '&' intersects and '~' introduces a
// regex.
const forbiddenHostNameChars = ",:!&~"

type inventoryHostResource struct {
	tc *terrapod.Client
}

// NewResource returns the terrapod_inventory_host resource.
func NewResource() resource.Resource {
	return &inventoryHostResource{}
}

func (r *inventoryHostResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_inventory_host"
}

func (r *inventoryHostResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Declares one host in a Terrapod workspace's ansible inventory. A host has a name and " +
			"nothing else — `ansible_host` is a variable like any other (`terrapod_inventory_host_var`), and its " +
			"group memberships are `terrapod_inventory_host_group` rows, so a second concern can put this host " +
			"in a group without owning the host. `for_each` over the instances a configuration creates (or over " +
			"the ones a configuration of nothing but data sources finds) is the documented shape, and gives each " +
			"host its own drift detection. See docs/ansible-inventory.md.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description:   "The host id (invhost-<uuid>).",
				Computed:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"workspace_id": schema.StringAttribute{
				Description: "The workspace whose inventory declares this host (e.g. \"ws-abc123\"). A host " +
					"belongs to one workspace, so moving it is a destroy and create.",
				Required:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"name": schema.StringAttribute{
				Description: "The inventory hostname (ansible's `inventory_hostname`), unique within the " +
					"workspace. Renaming is done in place, so this does not force a replacement — replacing a " +
					"host on a rename would briefly remove it from the target set. The name may not contain " +
					"whitespace or any of `, : ! & ~`: those are `--limit`'s own separators and operators, so " +
					"such a host could not be targeted and a leading `!` would silently exclude the host it names.",
				Required:   true,
				Validators: []validator.String{hostNameValidator{}},
			},
			"created_at": schema.StringAttribute{
				Description:   "When the host was declared (RFC3339).",
				Computed:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"updated_at": schema.StringAttribute{
				Description:   "When the host last changed (RFC3339).",
				Computed:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
		},
	}
}

func (r *inventoryHostResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *inventoryHostResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan inventoryHostModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	host, err := r.tc.CreateInventoryHost(ctx,
		plan.WorkspaceID.ValueString(), plan.Name.ValueString())
	if err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}
	readIntoModel(host, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *inventoryHostResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state inventoryHostModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	host, err := r.tc.GetInventoryHost(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}
	readIntoModel(host, &state)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *inventoryHostResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan inventoryHostModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	host, err := r.tc.UpdateInventoryHost(ctx, plan.ID.ValueString(), plan.Name.ValueString())
	if err != nil {
		resp.Diagnostics.AddError("Update failed", err.Error())
		return
	}
	readIntoModel(host, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *inventoryHostResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state inventoryHostModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	// Deleting a host cascades its variables and memberships server-side, so
	// those resources' own rows are already gone by the time Terraform asks.
	if err := r.tc.DeleteInventoryHost(ctx, state.ID.ValueString()); err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Delete failed", err.Error())
		}
	}
}

// ImportState takes the host id. The workspace comes back from the read.
func (r *inventoryHostResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

// ModifyPlan keeps a no-change re-plan empty. See
// planmods.KeepComputedWhenUnchanged for why the attribute plan modifiers
// cannot do this alone.
func (r *inventoryHostResource) ModifyPlan(ctx context.Context, req resource.ModifyPlanRequest, resp *resource.ModifyPlanResponse) {
	planmods.KeepComputedWhenUnchanged(ctx, req, resp,
		[]string{"workspace_id", "name"},
		[]string{"updated_at"},
	)
}

// readIntoModel projects the server's answer into the model.
func readIntoModel(h *terrapod.InventoryHost, m *inventoryHostModel) {
	m.ID = types.StringValue(h.ID)
	m.Name = types.StringValue(h.Name)
	m.CreatedAt = types.StringValue(h.CreatedAt)
	m.UpdatedAt = types.StringValue(h.UpdatedAt)
	// The configuration's own spelling of the workspace id is kept whenever it
	// names the workspace the server returned; otherwise the server wins and
	// the drift is reported (#1748).
	m.WorkspaceID = ids.Keep(m.WorkspaceID, h.WorkspaceID, "ws-")
}

type hostNameValidator struct{}

var _ validator.String = hostNameValidator{}

func (hostNameValidator) Description(_ context.Context) string {
	return "a host name must be non-empty and contain no whitespace or any of " + forbiddenHostNameChars
}

func (v hostNameValidator) MarkdownDescription(ctx context.Context) string { return v.Description(ctx) }

func (hostNameValidator) ValidateString(_ context.Context, req validator.StringRequest, resp *validator.StringResponse) {
	if req.ConfigValue.IsNull() || req.ConfigValue.IsUnknown() {
		return
	}
	if msg := hostNameProblem(req.ConfigValue.ValueString()); msg != "" {
		resp.Diagnostics.AddAttributeError(req.Path, "Invalid inventory host name", msg)
	}
}

// hostNameProblem returns why value cannot be targeted by ansible, or "".
//
// The server refuses the same set; checking it here turns an apply-time 422
// into a plan-time error that names the character.
func hostNameProblem(value string) string {
	if value == "" {
		return "a host name must not be empty"
	}
	if strings.IndexFunc(value, unicode.IsSpace) >= 0 {
		return fmt.Sprintf("host name %q contains whitespace, which --limit splits on, so the host "+
			"could not be targeted.", value)
	}
	if bad := badChars(value, forbiddenHostNameChars); bad != "" {
		return fmt.Sprintf("host name %q contains %q. Those are --limit's own operators and "+
			"separators: ',' and ':' separate patterns, '!' excludes, '&' intersects and '~' introduces "+
			"a regex. A host named with one of them cannot be selected, and a leading '!' would silently "+
			"exclude the host it names.", value, bad)
	}
	return ""
}

// badChars returns the distinct characters of value that appear in forbidden,
// sorted, or "".
func badChars(value, forbidden string) string {
	seen := map[rune]bool{}
	for _, c := range value {
		if strings.ContainsRune(forbidden, c) {
			seen[c] = true
		}
	}
	if len(seen) == 0 {
		return ""
	}
	out := make([]string, 0, len(seen))
	for c := range seen {
		out = append(out, string(c))
	}
	sort.Strings(out)
	return strings.Join(out, "")
}
