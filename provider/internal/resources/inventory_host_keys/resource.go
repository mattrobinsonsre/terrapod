package inventory_host_keys

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/attr"
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
	"github.com/mattrobinsonsre/terrapod/provider/internal/ids"
)

var (
	_ resource.Resource                = &inventoryHostKeysResource{}
	_ resource.ResourceWithImportState = &inventoryHostKeysResource{}
	_ resource.ResourceWithConfigure   = &inventoryHostKeysResource{}
)

// keyBlockType is the object a `key` block carries. Declared once so the
// schema, the state writer and the tests cannot disagree about it.
var keyBlockType = types.ObjectType{AttrTypes: map[string]attr.Type{
	"key_type":   types.StringType,
	"public_key": types.StringType,
	"marker":     types.StringType,
}}

type inventoryHostKeysResource struct {
	tc *terrapod.Client
}

// NewResource returns the terrapod_inventory_host_keys resource.
func NewResource() resource.Resource {
	return &inventoryHostKeysResource{}
}

func (r *inventoryHostKeysResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_inventory_host_keys"
}

func (r *inventoryHostKeysResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Declares the SSH host keys one inventory host is approved to present. Ansible has no " +
			"inventory-level model of an approved host key — its own `known_hosts` module manages a file on " +
			"a MANAGED node, and controller-side checking is a boolean plus whatever file the process finds " +
			"— so the trust set is Terrapod data and the runner renders it to a `known_hosts` file for " +
			"OpenSSH, which is the component that actually decides.\n\n" +
			"**This resource owns the host's whole key set.** One `key` block per key, and a key that " +
			"appears on the host out of band is drift that the next apply removes — which is the point, " +
			"because the alternative is a key nobody approved quietly becoming trusted. Declare one " +
			"`terrapod_inventory_host_keys` per host; two naming the same host will each remove the " +
			"other's keys.\n\n" +
			"**Declare every key the host offers, not one per algorithm family.** A client picks by its own " +
			"`HostKeyAlgorithms` preference, so a trust set holding one key per family refuses the host " +
			"whenever the two disagree. Two keys of ONE algorithm are also allowed, which is what makes a " +
			"rotation safe: declare the incoming key and mark the outgoing one `revoked`.\n\n" +
			"Compose it with the `terrapod_ssh_host_keys` data source using a `dynamic` block, since the " +
			"scan cannot supply a marker:\n\n" +
			"```hcl\n" +
			"resource \"terrapod_inventory_host_keys\" \"web1\" {\n" +
			"  host_id = terrapod_inventory_host.web1.id\n\n" +
			"  dynamic \"key\" {\n" +
			"    for_each = data.terrapod_ssh_host_keys.web1.entries[\"web-01.example.com\"]\n" +
			"    content {\n" +
			"      key_type   = key.value.key_type\n" +
			"      public_key = key.value.public_key\n" +
			"    }\n" +
			"  }\n" +
			"}\n" +
			"```\n\n" +
			"See docs/ansible-inventory.md.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description: "The host this trust set belongs to — the same value as `host_id`, because the " +
					"host IS this resource's identity. There is no separate object on the server to carry " +
					"an id of its own.",
				Computed:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"host_id": schema.StringAttribute{
				Description: "The host whose keys these are (e.g. \"invhost-abc123\"). A trust set belongs " +
					"to one host, so moving it is a destroy and create. Not defaulted from the provider's " +
					"`workspace` attribute: this is a HOST id, not a workspace id, so there is nothing for " +
					"TERRAPOD_WORKSPACE to supply — declare the host and reference it.",
				Required:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
		},
		Blocks: map[string]schema.Block{
			"key": schema.SetNestedBlock{
				Description: "One approved host key. Repeat the block per key; a SET, because a trust set " +
					"has no order and a list would plan a diff whenever two blocks were swapped. Declaring " +
					"no `key` blocks at all is a valid statement that this host is approved to present " +
					"nothing, and it removes any keys the host already has.",
				NestedObject: schema.NestedBlockObject{
					Attributes: map[string]schema.Attribute{
						"key_type": schema.StringAttribute{
							Description: "The algorithm, spelled as OpenSSH spells it in the second field " +
								"of a `known_hosts` line: `ssh-ed25519`, `ecdsa-sha2-nistp256`, " +
								"`ecdsa-sha2-nistp384`, `ecdsa-sha2-nistp521`, `ssh-rsa`, `rsa-sha2-256` " +
								"or `rsa-sha2-512`. The server validates it against that closed set, and " +
								"the reason is the direction a typo fails in: an unrecognised algorithm " +
								"in a trust anchor silently never matches, and the symptom reads as the " +
								"host having rotated its key rather than as a configuration error.",
							Required: true,
						},
						"public_key": schema.StringAttribute{
							Description: "The base64 blob ALONE — the third field of a `known_hosts` " +
								"line, with no algorithm prefix and no host. The server refuses a whole " +
								"line, because stored as the blob it would render with the host and " +
								"algorithm doubled and nothing would match it; it also checks the blob's " +
								"own embedded algorithm name against `key_type`. Not marked sensitive: a " +
								"host's public key is public, and hiding it would hide the one value an " +
								"operator needs to read when a host presents something else.",
							Required: true,
						},
						"marker": schema.StringAttribute{
							Description: "The optional first field of a `known_hosts` line, WITHOUT its " +
								"`@`: `cert-authority` or `revoked`. Omit it for an ordinary entry.\n\n" +
								"Per key rather than per algorithm, which is what makes `revoked` " +
								"useful: the outgoing key of a rotation stays declared and is refused, so " +
								"a replay of it is rejected rather than silently treated as an unknown " +
								"host. Changing only the marker is applied in place, so a rotation never " +
								"leaves the host momentarily untrusted.",
							Optional:   true,
							Validators: []validator.String{markerValidator{}},
						},
					},
				},
			},
		},
	}
}

func (r *inventoryHostKeysResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *inventoryHostKeysResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan inventoryHostKeysModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	desired, diags := blocksOf(ctx, plan.Keys)
	resp.Diagnostics.Append(diags...)
	if resp.Diagnostics.HasError() {
		return
	}

	hostID := plan.HostID.ValueString()
	for _, want := range desired {
		if _, err := r.tc.CreateInventoryHostKey(ctx, hostID, createRequestOf(want)); err != nil {
			resp.Diagnostics.AddError("Create failed",
				fmt.Sprintf("declaring %s key %s: %s",
					want.KeyType.ValueString(), shortKey(want.PublicKey.ValueString()), err))
			return
		}
	}

	// The configuration's own blocks go back into state rather than a re-read:
	// a set element must equal the one the plan held, and the server answers
	// with the same values it was sent (the marker's one normalisation, "" to
	// null, is the projection readIntoModel already applies).
	plan.ID = plan.HostID
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *inventoryHostKeysResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state inventoryHostKeysModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	rows, err := r.tc.ListInventoryHostKeys(ctx, state.HostID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			// There is no "trust set" object to go missing: this resource exists
			// for as long as its HOST does, and the list route 404s once the
			// host is gone. Deleting the host cascades its keys, so this is the
			// ordinary way the resource disappears.
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}

	resp.Diagnostics.Append(readIntoModel(rows, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *inventoryHostKeysResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan inventoryHostKeysModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	desired, diags := blocksOf(ctx, plan.Keys)
	resp.Diagnostics.Append(diags...)
	if resp.Diagnostics.HasError() {
		return
	}

	hostID := plan.HostID.ValueString()
	actual, err := r.tc.ListInventoryHostKeys(ctx, hostID)
	if err != nil {
		resp.Diagnostics.AddError("Update failed",
			fmt.Sprintf("reading the host's current keys: %s", err))
		return
	}

	changes := planChanges(desired, actual)

	// CREATE first, then re-mark, then DELETE. The order is the point: a trust
	// set is never momentarily empty, so a rotation cannot leave a window in
	// which the host presents a key nothing approves. The row's identity is
	// (host, algorithm, blob), so an incoming key of an algorithm already
	// present does not collide with the outgoing one.
	for _, want := range changes.create {
		if _, err := r.tc.CreateInventoryHostKey(ctx, hostID, createRequestOf(want)); err != nil {
			resp.Diagnostics.AddError("Update failed",
				fmt.Sprintf("declaring %s key %s: %s",
					want.KeyType.ValueString(), shortKey(want.PublicKey.ValueString()), err))
			return
		}
	}
	for _, m := range changes.remark {
		marker := m.marker
		if _, err := r.tc.UpdateInventoryHostKey(ctx, m.id,
			terrapod.UpdateInventoryHostKeyRequest{Marker: &marker}); err != nil {
			resp.Diagnostics.AddError("Update failed",
				fmt.Sprintf("re-marking key %s: %s", m.id, err))
			return
		}
	}
	for _, id := range changes.delete {
		if err := r.tc.DeleteInventoryHostKey(ctx, id); err != nil {
			var nf *terrapod.NotFoundError
			if errors.As(err, &nf) {
				continue
			}
			resp.Diagnostics.AddError("Update failed",
				fmt.Sprintf("withdrawing key %s: %s", id, err))
			return
		}
	}

	plan.ID = plan.HostID
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *inventoryHostKeysResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state inventoryHostKeysModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	// Every key the host currently holds, not just the ones state remembers:
	// this resource owns the host's trust set, so destroying it leaves no
	// approved key behind — including one added out of band, which state could
	// not know about.
	rows, err := r.tc.ListInventoryHostKeys(ctx, state.HostID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			// The host is already gone and took its keys with it.
			return
		}
		resp.Diagnostics.AddError("Delete failed",
			fmt.Sprintf("reading the host's current keys: %s", err))
		return
	}
	for _, row := range rows {
		if err := r.tc.DeleteInventoryHostKey(ctx, row.ID); err != nil {
			var nf *terrapod.NotFoundError
			if errors.As(err, &nf) {
				continue
			}
			resp.Diagnostics.AddError("Delete failed",
				fmt.Sprintf("withdrawing key %s: %s", row.ID, err))
			return
		}
	}
}

// ImportState takes the HOST id, which is this resource's identity. Both `id`
// and `host_id` are set from it, because the read needs the host to list from
// and `id` is the same value.
func (r *inventoryHostKeysResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), req.ID)...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("host_id"), req.ID)...)
}

// ── The set diff ─────────────────────────────────────────────────────────────

// markerChange is one row whose marker is to be replaced in place.
type markerChange struct {
	id     string
	marker string
}

// keySetChanges is what an update has to do to reach the declared trust set.
type keySetChanges struct {
	create []hostKeyBlockModel
	remark []markerChange
	delete []string
}

// planChanges works out the minimal set of calls that takes `actual` to
// `desired`.
//
// A row is matched on (key_type, public_key) because that is its identity on
// the server — two thirds of its unique constraint — and the marker is the one
// thing about it that can be changed in place. So a marker-only edit is a PATCH
// rather than a destroy and declare, which matters for a trust anchor: the
// replace would withdraw the key and re-add it, and between the two the host
// presents something nothing approves.
//
// A duplicate (key_type, public_key) among the desired blocks is impossible to
// reach -- Terraform de-duplicates a set -- so matching consumes each actual
// row at most once and nothing has to decide which of two identical blocks
// won.
func planChanges(desired []hostKeyBlockModel, actual []terrapod.InventoryHostKey) keySetChanges {
	type rowKey struct{ keyType, publicKey string }

	remaining := make(map[rowKey]terrapod.InventoryHostKey, len(actual))
	for _, row := range actual {
		remaining[rowKey{row.KeyType, row.PublicKey}] = row
	}

	var changes keySetChanges
	for _, want := range desired {
		k := rowKey{want.KeyType.ValueString(), want.PublicKey.ValueString()}
		row, found := remaining[k]
		if !found {
			changes.create = append(changes.create, want)
			continue
		}
		delete(remaining, k)
		// markerOf collapses null and "" to "", which is what the server
		// stores for an ordinary entry — so an absent block attribute and an
		// unmarked row compare equal and nothing is re-marked for nothing.
		if row.Marker != markerOf(want.Marker) {
			changes.remark = append(changes.remark, markerChange{row.ID, markerOf(want.Marker)})
		}
	}
	for _, row := range remaining {
		changes.delete = append(changes.delete, row.ID)
	}
	// Sorted so the calls an apply makes are reproducible and a test can assert
	// them: a map's iteration order is not.
	sort.Strings(changes.delete)
	return changes
}

// ── Projection ───────────────────────────────────────────────────────────────

func createRequestOf(b hostKeyBlockModel) terrapod.CreateInventoryHostKeyRequest {
	return terrapod.CreateInventoryHostKeyRequest{
		KeyType:   b.KeyType.ValueString(),
		PublicKey: b.PublicKey.ValueString(),
		Marker:    markerOf(b.Marker),
	}
}

// markerOf reads a block's marker as the server stores it: null and "" are both
// an ordinary entry.
func markerOf(v types.String) string {
	if v.IsNull() || v.IsUnknown() {
		return ""
	}
	return v.ValueString()
}

// markerValue is the inverse: the server's empty marker is an ABSENT block
// attribute, not an empty string.
//
// Projecting it as "" instead would make an unmarked row differ from the
// configuration that omitted the attribute, and a set element that differs is
// a set element Terraform cannot match — a permanent diff on every unmarked
// key, which is almost all of them.
func markerValue(stored string) types.String {
	if stored == "" {
		return types.StringNull()
	}
	return types.StringValue(stored)
}

// readIntoModel projects the host's current keys into the model.
func readIntoModel(rows []terrapod.InventoryHostKey, m *inventoryHostKeysModel) diag.Diagnostics {
	elems := make([]attr.Value, 0, len(rows))
	for _, row := range rows {
		obj, diags := types.ObjectValue(keyBlockType.AttrTypes, map[string]attr.Value{
			"key_type":   types.StringValue(row.KeyType),
			"public_key": types.StringValue(row.PublicKey),
			"marker":     markerValue(row.Marker),
		})
		if diags.HasError() {
			return diags
		}
		elems = append(elems, obj)
	}
	set, diags := types.SetValue(keyBlockType, elems)
	if diags.HasError() {
		return diags
	}
	m.Keys = set

	// The host id every row agrees on, so the configuration's own spelling is
	// kept whenever it names that host (#1748) — this attribute forces a
	// replacement, so a drifted spelling would destroy and recreate the trust
	// set. With no rows there is nothing to compare against and state keeps
	// what it has, which is the honest answer rather than a guess.
	if len(rows) > 0 {
		m.HostID = ids.Keep(m.HostID, rows[0].HostID, "invhost-")
	}
	if m.ID.IsNull() || m.ID.IsUnknown() {
		m.ID = m.HostID
	}
	return nil
}

// blocksOf reads the `key` blocks out of the set.
func blocksOf(ctx context.Context, set types.Set) ([]hostKeyBlockModel, diag.Diagnostics) {
	// A null or unknown set is no blocks. Terraform renders an absent block as
	// an empty set rather than null, but a value arriving null must not be read
	// as an error — and "no blocks" is a legitimate declaration.
	if set.IsNull() || set.IsUnknown() {
		return nil, nil
	}
	var out []hostKeyBlockModel
	diags := set.ElementsAs(ctx, &out, false)
	return out, diags
}

// shortKey abbreviates a blob for an error message. The whole thing is several
// hundred characters and the leading bytes are enough to tell two keys apart.
func shortKey(blob string) string {
	const keep = 16
	if len(blob) <= keep {
		return blob
	}
	return blob[:keep] + "…"
}

// ── Validation ───────────────────────────────────────────────────────────────

// markerValidator refuses a marker OpenSSH does not define.
//
// The server refuses the same two-value set; checking it here turns an
// apply-time 422 into a plan-time error that names what it expected. It also
// closes a hole an absent validator would leave: `marker = ""` would be
// accepted, normalised to null by the server, and then differ from the
// configuration on every subsequent plan.
//
// The set is OpenSSH's own rather than Terrapod's, so it does not move; and if
// OpenSSH ever defines a third, widening this is additive.
type markerValidator struct{}

func (markerValidator) Description(_ context.Context) string {
	return fmt.Sprintf("must be one of %s, without the leading '@'",
		strings.Join(knownHostsMarkers, " or "))
}

func (v markerValidator) MarkdownDescription(ctx context.Context) string { return v.Description(ctx) }

func (markerValidator) ValidateString(_ context.Context, req validator.StringRequest, resp *validator.StringResponse) {
	if req.ConfigValue.IsNull() || req.ConfigValue.IsUnknown() {
		return
	}
	if msg := markerProblem(req.ConfigValue.ValueString()); msg != "" {
		resp.Diagnostics.AddAttributeError(req.Path, "Invalid known_hosts marker", msg)
	}
}

// knownHostsMarkers is the set OpenSSH defines, without the leading '@'.
var knownHostsMarkers = []string{"cert-authority", "revoked"}

// markerProblem returns why value is not a known_hosts marker, or "".
func markerProblem(value string) string {
	if value == "" {
		return "a marker must not be empty. Omit the attribute entirely for an ordinary entry — " +
			"the server stores an empty marker as no marker, so setting it to \"\" would differ " +
			"from the configuration on every plan."
	}
	for _, known := range knownHostsMarkers {
		if value == known {
			return ""
		}
	}
	return fmt.Sprintf("marker %q is not one OpenSSH defines. Expected %s, without the leading "+
		"'@' — Terrapod adds that when it renders the line.",
		value, strings.Join(knownHostsMarkers, " or "))
}
