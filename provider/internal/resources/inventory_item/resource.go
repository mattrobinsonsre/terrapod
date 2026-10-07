package inventory_item

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"unicode"

	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/schema/validator"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var (
	_ resource.Resource                = &inventoryItemResource{}
	_ resource.ResourceWithImportState = &inventoryItemResource{}
)

type inventoryItemResource struct {
	tc *terrapod.Client
}

// NewResource returns the terrapod_inventory_item resource.
func NewResource() resource.Resource {
	return &inventoryItemResource{}
}

func (r *inventoryItemResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_inventory_item"
}

func (r *inventoryItemResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Declares one ansible host in a Terrapod workspace's inventory — the \"inventory from the " +
			"managing Terraform\" source. One resource per host, so `for_each` over the instances a configuration " +
			"creates (or over the ones a configuration of nothing but data sources finds) declares them with their " +
			"groups and variables, and each host gets its own drift detection.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description:   "The inventory item ID (invitem-<uuid>).",
				Computed:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"workspace_id": schema.StringAttribute{
				Description: "The workspace whose inventory declares this host (e.g. \"ws-abc123\"). An item belongs " +
					"to one workspace, so moving it is a destroy and create.",
				Required:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"name": schema.StringAttribute{
				Description: "The inventory hostname, unique within the workspace. Renaming is done in place, so " +
					"this does not force a replacement; a name another host already holds is refused. The name may " +
					"not contain whitespace or any of `, : ! & ~` — those are `--limit`'s own separators and " +
					"operators, so such a host could not be targeted and a leading `!` would silently exclude the " +
					"host it names.",
				Required:   true,
				Validators: []validator.String{hostNameValidator{}},
			},
			"address": schema.StringAttribute{
				Description: "Populates `ansible_host`. Optional: a host whose name already resolves needs none. An " +
					"explicit `ansible_host` in `vars` wins over this.",
				Optional: true,
			},
			"groups": schema.SetAttribute{
				Description: "Group names this host belongs to. Never `all` or `ungrouped`: ansible derives both, so " +
					"a declaration cannot control either one's membership. Removing the attribute puts the host in no " +
					"groups — nothing else writes one host's membership, so there is no \"leave it alone\" reading " +
					"for this to mean.",
				Optional:    true,
				ElementType: types.StringType,
				Validators:  []validator.Set{groupNamesValidator{}},
			},
			"vars": schema.MapAttribute{
				Description: "Ansible host variables. `ansible_host`, `ansible_user` and friends live here like any " +
					"other — Terrapod gives them no special meaning, because ansible does not either. Values are " +
					"strings: a variable that has to be a list or a number belongs in the playbook repository's " +
					"`group_vars`/`host_vars`, which is where an ansible operator keeps such a thing anyway.",
				Optional:    true,
				ElementType: types.StringType,
				Validators:  []validator.Map{varNamesValidator{}},
			},
			"created_at": schema.StringAttribute{
				Description:   "Creation timestamp.",
				Computed:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"updated_at": schema.StringAttribute{
				Description: "Last update timestamp.",
				Computed:    true,
			},
		},
	}
}

func (r *inventoryItemResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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
	r.tc = tc
}

func (r *inventoryItemResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan inventoryItemModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	createReq, diags := buildCreateRequest(ctx, &plan)
	resp.Diagnostics.Append(diags...)
	if resp.Diagnostics.HasError() {
		return
	}
	item, err := r.tc.CreateInventoryItem(ctx, plan.WorkspaceID.ValueString(), createReq)
	if err != nil {
		resp.Diagnostics.AddError("Failed to declare the inventory item", err.Error())
		return
	}
	resp.Diagnostics.Append(readIntoModel(ctx, item, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *inventoryItemResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state inventoryItemModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	item, err := r.tc.GetInventoryItem(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Failed to read the inventory item", err.Error())
		return
	}
	resp.Diagnostics.Append(readIntoModel(ctx, item, &state)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *inventoryItemResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state inventoryItemModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	updateReq, diags := buildUpdateRequest(ctx, &plan)
	resp.Diagnostics.Append(diags...)
	if resp.Diagnostics.HasError() {
		return
	}
	item, err := r.tc.UpdateInventoryItem(ctx, state.ID.ValueString(), updateReq)
	if err != nil {
		resp.Diagnostics.AddError("Failed to update the inventory item", err.Error())
		return
	}
	resp.Diagnostics.Append(readIntoModel(ctx, item, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *inventoryItemResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state inventoryItemModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.tc.DeleteInventoryItem(ctx, state.ID.ValueString()); err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Failed to delete the inventory item", err.Error())
		}
	}
}

func (r *inventoryItemResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

// buildCreateRequest omits what was not configured, so the server's own empty
// defaults apply rather than an explicit empty being sent for every host.
func buildCreateRequest(ctx context.Context, m *inventoryItemModel) (terrapod.CreateInventoryItemRequest, diag.Diagnostics) {
	var diags diag.Diagnostics
	req := terrapod.CreateInventoryItemRequest{
		Name:    m.Name.ValueString(),
		Address: m.Address.ValueString(),
	}
	if configured(m.Groups) {
		groups := []string{}
		diags.Append(m.Groups.ElementsAs(ctx, &groups, false)...)
		req.Groups = groups
	}
	if configured(m.Vars) {
		hostVars := map[string]string{}
		diags.Append(m.Vars.ElementsAs(ctx, &hostVars, false)...)
		req.Vars = hostVars
	}
	return req, diags
}

// buildUpdateRequest converges the host on the configuration, so every mutable
// field is sent on every update.
//
// That is what makes a removed attribute mean something. The SDK's pointers
// distinguish "leave alone" (nil) from "set or clear" (a pointer, empty for a
// clear), and this resource never wants the first: nothing but this
// configuration writes one host's address, groups or variables, so an attribute
// gone from the config is an instruction to clear it and not an absence of
// instruction. Omitting it instead would make a host impossible to remove from
// its groups without declaring `groups = []`, which is the wrong half of the
// distinction the API went to the trouble of offering.
//
// An unknown value is the one exception: it is sent as nil, because a value
// Terraform could not resolve is not a request to clear anything.
func buildUpdateRequest(ctx context.Context, m *inventoryItemModel) (terrapod.UpdateInventoryItemRequest, diag.Diagnostics) {
	var diags diag.Diagnostics
	req := terrapod.UpdateInventoryItemRequest{Name: m.Name.ValueString()}

	if !m.Address.IsUnknown() {
		// Null reads as "" here, which is the clearing form: the server stores
		// an absent address as the empty string.
		address := m.Address.ValueString()
		req.Address = &address
	}
	if !m.Groups.IsUnknown() {
		groups := []string{}
		if !m.Groups.IsNull() {
			diags.Append(m.Groups.ElementsAs(ctx, &groups, false)...)
		}
		req.Groups = &groups
	}
	if !m.Vars.IsUnknown() {
		hostVars := map[string]string{}
		if !m.Vars.IsNull() {
			diags.Append(m.Vars.ElementsAs(ctx, &hostVars, false)...)
		}
		req.Vars = &hostVars
	}
	return req, diags
}

// readIntoModel copies the server's item into the model, keeping the form each
// field was configured in wherever the server's value is that form's equal.
//
// The server stores an absent address, group list or variable map as an empty
// one, so null and empty are the same request; writing either form over the
// other says nothing and fails the apply as "Provider produced inconsistent
// result after apply" (#1634, in the collection and scalar cases).
func readIntoModel(ctx context.Context, item *terrapod.InventoryItem, m *inventoryItemModel) diag.Diagnostics {
	var diags diag.Diagnostics

	m.ID = types.StringValue(item.ID)

	// The workspace comes back as a prefixed id. A configuration that named the
	// bare uuid keeps that form, so neither spelling drifts; a genuinely
	// different workspace (and an import, which starts with nothing) takes the
	// server's value, which is what lets an imported item plan clean.
	if item.WorkspaceID != "" &&
		(m.WorkspaceID.IsNull() || m.WorkspaceID.IsUnknown() ||
			!sameWorkspace(m.WorkspaceID.ValueString(), item.WorkspaceID)) {
		m.WorkspaceID = types.StringValue(item.WorkspaceID)
	}

	m.Name = types.StringValue(item.Name)
	m.Address = keepEmptyForm(m.Address, item.Address)

	if len(item.Groups) == 0 {
		// A configured empty set is left exactly as it is; anything else reads
		// as null, which is the same request said the other way.
		if !isEmptyNotNull(m.Groups.IsNull(), m.Groups.IsUnknown(), len(m.Groups.Elements())) {
			m.Groups = types.SetNull(types.StringType)
		}
	} else {
		sv, d := types.SetValueFrom(ctx, types.StringType, item.Groups)
		diags.Append(d...)
		m.Groups = sv
	}

	if len(item.Vars) == 0 {
		if !isEmptyNotNull(m.Vars.IsNull(), m.Vars.IsUnknown(), len(m.Vars.Elements())) {
			m.Vars = types.MapNull(types.StringType)
		}
	} else {
		mv, d := types.MapValueFrom(ctx, types.StringType, item.Vars)
		diags.Append(d...)
		m.Vars = mv
	}

	m.CreatedAt = types.StringValue(item.CreatedAt)
	m.UpdatedAt = types.StringValue(item.UpdatedAt)
	return diags
}

// configured reports whether an attribute holds a value the practitioner wrote.
func configured(v interface {
	IsNull() bool
	IsUnknown() bool
}) bool {
	return !v.IsNull() && !v.IsUnknown()
}

// keepEmptyForm keeps a configured "" rather than replacing it with null, and
// vice versa, when the server's value is empty.
func keepEmptyForm(current types.String, server string) types.String {
	if server != "" {
		return types.StringValue(server)
	}
	if current.IsNull() || (!current.IsUnknown() && current.ValueString() == "") {
		return current
	}
	return types.StringNull()
}

// isEmptyNotNull reports whether a collection already holds a configured empty
// value, which the server's empty one agrees with and must not overwrite.
//
// Takes the three facts rather than the value because a Set's Elements() is a
// slice and a Map's is a map, so one signature cannot read both.
func isEmptyNotNull(isNull, isUnknown bool, elements int) bool {
	return !isNull && !isUnknown && elements == 0
}

// sameWorkspace reports whether two workspace ids name the same workspace, with
// or without the "ws-" prefix.
func sameWorkspace(a, b string) bool {
	return strings.TrimPrefix(a, "ws-") == strings.TrimPrefix(b, "ws-")
}

// ── Plan-time validation ─────────────────────────────────────────────────────
//
// The server refuses all of this too, with a 422 naming the offending value,
// and that check is the authority -- the API is public and a provider is not
// its only client. These exist because the server's refusal arrives during
// APPLY, and the declared-inventory shape is `for_each` over a host map: one
// bad name means the apply stops partway through five hundred hosts, having
// already declared some of them. A name's usability as an ansible target is a
// shape constraint, which is what a schema is for, so saying it here turns that
// into a plan-time error naming the attribute.
//
// Written against the framework's own interfaces rather than pulled from
// terraform-plugin-framework-validators, which this module does not depend on
// (see workspace/oidc_audiences_validator.go for the same call).

// Characters that make a host name unusable as a target rather than merely
// ugly, mirroring the server's own set: `,` and `:` separate patterns in
// `--limit`, and `!`, `&` and `~` are its exclusion, intersection and regex
// operators.
const forbiddenHostNameChars = ",:!&~"

// What ansible accepts as a group name without warning, mirroring the server.
// Ansible is laxer in practice, but a name it warns about cannot be used
// reliably in a `--limit` pattern or a `group_vars` filename.
var groupNamePattern = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

// derivedGroups are computed by ansible rather than declared: `all` holds every
// host and `ungrouped` the hosts in no other group, so a declaration cannot
// control either one's membership.
var derivedGroups = map[string]bool{"all": true, "ungrouped": true}

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

type groupNamesValidator struct{}

var _ validator.Set = groupNamesValidator{}

func (groupNamesValidator) Description(_ context.Context) string {
	return "a group name must be an identifier, and never `all` or `ungrouped`"
}

func (v groupNamesValidator) MarkdownDescription(ctx context.Context) string {
	return v.Description(ctx)
}

func (groupNamesValidator) ValidateSet(_ context.Context, req validator.SetRequest, resp *validator.SetResponse) {
	if req.ConfigValue.IsNull() || req.ConfigValue.IsUnknown() {
		return
	}
	// Sorted, so a configuration with several offending names reports them in a
	// stable order across runs of the same plan.
	problems := map[string]string{}
	for _, raw := range req.ConfigValue.Elements() {
		s, ok := raw.(types.String)
		if !ok || s.IsNull() || s.IsUnknown() {
			continue
		}
		if msg := groupNameProblem(s.ValueString()); msg != "" {
			problems[s.ValueString()] = msg
		}
	}
	for _, name := range sortedKeys(problems) {
		resp.Diagnostics.AddAttributeError(req.Path, "Invalid inventory group name", problems[name])
	}
}

// groupNameProblem returns why value cannot be an ansible group name, or "".
func groupNameProblem(value string) string {
	if value == "" {
		return "a group name must not be empty"
	}
	if derivedGroups[value] {
		return fmt.Sprintf("%q is derived by ansible, not declared: 'all' holds every host and "+
			"'ungrouped' holds the hosts in no other group, so a declaration cannot control either "+
			"one's membership. Name a group of your own instead.", value)
	}
	if !groupNamePattern.MatchString(value) {
		return fmt.Sprintf("group name %q must start with a letter or underscore and contain only "+
			"letters, digits and underscores. Ansible tolerates more than that but warns, and a name it "+
			"warns about cannot be used reliably in a --limit pattern or a group_vars filename.", value)
	}
	return ""
}

type varNamesValidator struct{}

var _ validator.Map = varNamesValidator{}

func (varNamesValidator) Description(_ context.Context) string {
	return "a host variable name must be non-empty"
}

func (v varNamesValidator) MarkdownDescription(ctx context.Context) string { return v.Description(ctx) }

// ValidateMap refuses only an empty key, deliberately laxer than the group
// rule. Ansible stores a variable whose name is not an identifier and only
// warns that it is unreachable as `{{ name }}`; it is still readable via
// `hostvars['h']['odd-name']`, which some roles do on purpose. So refusing more
// would block working configurations to prevent a warning.
func (varNamesValidator) ValidateMap(_ context.Context, req validator.MapRequest, resp *validator.MapResponse) {
	if req.ConfigValue.IsNull() || req.ConfigValue.IsUnknown() {
		return
	}
	if _, ok := req.ConfigValue.Elements()[""]; ok {
		resp.Diagnostics.AddAttributeError(req.Path, "Invalid host variable name",
			"a host variable name must be a non-empty string")
	}
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

func sortedKeys(m map[string]string) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
