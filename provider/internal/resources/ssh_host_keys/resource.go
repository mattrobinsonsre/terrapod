// Package ssh_host_keys implements the terrapod_ssh_host_keys resource (#2592):
// scan a fleet's SSH host keys once, PIN them, and render the `known_hosts`
// body that a machine credential's `known_hosts` field takes.
//
// WHY IT EXISTS. Host-key verification is on for a configure and has always
// been — ansible's own `HOST_KEY_CHECKING` defaults true, the configure phase
// forces nothing about it, and the execution environment ships no
// `known_hosts` — so first contact with a host nobody has vouched for fails
// closed. #2318 added the `known_hosts` field to make that default
// SATISFIABLE; its own note is the reason this exists: "a correct default
// nobody can satisfy becomes a default everybody turns off". Satisfying it by
// hand means obtaining and pasting a key per host, and an operator with a
// fleet reaches for `ansible_host_key_checking = false` instead. So the
// baseline this is measured against is not out-of-band key distribution, it is
// verification switched OFF — and against that baseline, scan-and-pin detects
// a later key change where `false` detects nothing, ever.
//
// It is still trust-on-first-use, and weaker than distributing keys out of
// band. That is stated here and in docs/ansible-configure.md rather than
// smoothed over.
//
// WHY A RESOURCE AND NOT A DATA SOURCE, which is what #2592's title asks for.
// The governing requirement on the issue is that "an unreachable host needs to
// answer from state and not fail". `datasource.ReadRequest` carries only
// `Config` — a data source has no prior state at Read time — so "answer from
// state" is not expressible in one at all. A managed resource's Read receives
// prior `State` and can keep a value. The requirement therefore forces a
// resource; this is a finding about the framework rather than a liberty taken
// with the title.
//
// TWO FIRSTS FOR THIS PROVIDER, both deliberate.
//
//   - It is the first resource that OWNS NOTHING ON THE SERVER. There is no
//     Configure, no go-terrapod client and no API call anywhere in this
//     package: Create, Read and Update compute state, and Delete discards it.
//     That is the same local-state-holding shape as `terraform_data` or
//     `tls_private_key`, and it is what lets the pin live in Terraform state —
//     which is the only place it can live, because the pin has to be compared
//     against on a later read and the server has no opinion about it.
//   - It is the first that OPENS A SOCKET TO A THIRD PARTY. Every other
//     resource and data source talks to the Terrapod API and nothing else.
//     That is why `golang.org/x/crypto` is this module's one non-HashiCorp,
//     non-Terrapod direct dependency: the provider's dependency surface is
//     deliberately thin, and the alternative was shelling out to
//     `ssh-keyscan`, which may not be on a runner's PATH. The repository
//     already carries x/crypto v0.58.0 in three other modules, so this adds a
//     direct requirement rather than a new version to audit.
//
// WHERE THE SCAN RUNS MATTERS. In agent mode the provider runs on the runner,
// which is the same network position the execution environment will later
// connect from — the right vantage point. In local mode it runs on the
// operator's own machine, which may not reach the fleet at all. "It worked on
// my laptop and the plan is empty on the runner" is a confusing first
// experience, so the unreachable diagnostic names it.
package ssh_host_keys

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"strings"
	"time"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64default"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/listdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

var _ resource.Resource = &sshHostKeysResource{}

// defaultPort is SSH's own, and the value `port` defaults to.
const defaultPort int64 = 22

// defaultScanTimeoutSeconds bounds one connection attempt. Five seconds is
// enough for a host on the same network to complete a key exchange and short
// enough that a fleet with a handful of dead hosts does not make a plan feel
// hung — and the worst case per host is this times the number of `key_types`,
// because each algorithm is its own connection.
const defaultScanTimeoutSeconds int64 = 5

type sshHostKeysResource struct {
	// fetch is the network half, held as a field so tests substitute it. The
	// production value is dialForHostKeys; nothing configures it, because there
	// is nothing about it an operator should be choosing.
	fetch hostKeyFetcher
}

type sshHostKeysModel struct {
	ID                 types.String `tfsdk:"id"`
	Hosts              types.Set    `tfsdk:"hosts"`
	Port               types.Int64  `tfsdk:"port"`
	KeyTypes           types.List   `tfsdk:"key_types"`
	ScanTimeoutSeconds types.Int64  `tfsdk:"scan_timeout_seconds"`
	PinnedEntries      types.Map    `tfsdk:"pinned_entries"`
	KnownHosts         types.String `tfsdk:"known_hosts"`
	Entries            types.Map    `tfsdk:"entries"`
	UnreachableHosts   types.Set    `tfsdk:"unreachable_hosts"`
	ChangedHosts       types.Set    `tfsdk:"changed_hosts"`
	ScannedAt          types.String `tfsdk:"scanned_at"`
}

// NewResource returns the terrapod_ssh_host_keys resource.
func NewResource() resource.Resource {
	return &sshHostKeysResource{fetch: dialForHostKeys}
}

func (r *sshHostKeysResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_ssh_host_keys"
}

func (r *sshHostKeysResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Scans a fleet's SSH host keys, PINS them in Terraform state, and renders the " +
			"`known_hosts` body a machine credential's `known_hosts` field takes — so host-key " +
			"verification can be satisfied for a whole inventory instead of being turned off. Compose it " +
			"with `data.terrapod_inventory_resolved` for the host list and a `terrapod_variable` of " +
			"category `ansible_machine_auth` for the destination. It owns nothing on the server: it opens " +
			"an SSH connection per host per key type, keeps what it finds, and stores the result locally. " +
			"A later read NEVER overwrites a pinned key — a host answering differently is reported in " +
			"`changed_hosts` and the pin is kept, because accepting a new key silently is what SSH already does " +
			"after first contact and is the thing pinning exists to stop. An unreachable host keeps what " +
			"it had rather than failing the read. Scanning is trust-on-first-use and weaker than " +
			"distributing keys out of band; it is stronger than the `ansible_host_key_checking = false` " +
			"it realistically replaces. See docs/ansible-configure.md.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description: "An opaque local identifier, assigned once. There is nothing on the server " +
					"for it to be the id OF, so it is deliberately random rather than derived from the " +
					"configuration: a hash of the host set would change when a host was added, which in a " +
					"state viewer reads as the resource having been replaced when it has not.",
				Computed:      true,
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"hosts": schema.SetAttribute{
				Description: "The hosts to scan, each `host` or `host:port` (a bracketed `[::1]:2222` for " +
					"an IPv6 literal with a port). A per-entry port overrides `port`. An entry is used " +
					"verbatim as the key of `entries`, `unreachable_hosts` and `changed_hosts` and of " +
					"`pinned_entries`, so rewriting one — `web1` to `web1:22`, say — re-pins that host " +
					"even though it addresses the same machine. NOTE that an inventory NAME is not " +
					"necessarily resolvable: take `ansible_host` where the inventory sets it, and the " +
					"inventory name only otherwise.",
				Required:    true,
				ElementType: types.StringType,
			},
			"port": schema.Int64Attribute{
				Description: "The port for entries of `hosts` that do not carry one. Defaults to 22.",
				Optional:    true,
				Computed:    true,
				Default:     int64default.StaticInt64(defaultPort),
			},
			"key_types": schema.ListAttribute{
				Description: "The SSH host-key algorithms to ask each host for, in order. These are " +
					"algorithm names, not `ssh-keyscan`'s short `-t` aliases: \"ssh-ed25519\", not " +
					"\"ed25519\". Accepted: " + strings.Join(supportedKeyTypes, ", ") + ". Defaults to " +
					strings.Join(defaultKeyTypes, ", ") + " — one current algorithm per key family, with " +
					"`ssh-rsa` left out because it is RSA over SHA-1, which a current sshd refuses by " +
					"default, and `rsa-sha2-512` reaches the same RSA key. `rsa-sha2-256` and " +
					"`rsa-sha2-512` are two signature algorithms over ONE `ssh-rsa` key, so asking for " +
					"both yields one entry. This decides what the scan ASKS FOR and not what stays " +
					"pinned: narrowing it stops new key types being added and un-pins nothing.",
				Optional:    true,
				Computed:    true,
				ElementType: types.StringType,
				Default:     listdefault.StaticValue(defaultKeyTypesValue()),
			},
			"scan_timeout_seconds": schema.Int64Attribute{
				Description: "How long one connection attempt may take, in seconds. Defaults to 5. Each " +
					"key type is its own connection — which is how `ssh-keyscan` works, because the " +
					"server picks one algorithm per handshake — so the worst case for a wholly " +
					"unresponsive host is this times the length of `key_types`.",
				Optional: true,
				Computed: true,
				Default:  int64default.StaticInt64(defaultScanTimeoutSeconds),
			},
			"pinned_entries": schema.MapAttribute{
				Description: "Hand-written `known_hosts` line(s) per host, which WIN over anything " +
					"scanned: a host named here is never scanned at all. This is the answer to \"if there " +
					"isn't one already\" — you declare what you already trust, and the scanner fills in " +
					"the rest. A value may hold several lines, for a host offering several key types; " +
					"blank lines and `#` comments are dropped and everything else is kept verbatim, so a " +
					"`@cert-authority` marker or a trailing comment survives. The map KEY is how this " +
					"resource addresses the entry and must match an entry of `hosts`; what ssh matches on " +
					"is the line's own host field, which may legitimately be a comma-separated list, a " +
					"`*` pattern or a hashed host, so the two are deliberately not required to agree.",
				Optional:    true,
				ElementType: types.StringType,
			},
			"known_hosts": schema.StringAttribute{
				Description: "The rendered `known_hosts` file: every pinned line, hosts in sorted order, " +
					"one per line with a trailing newline. This is what feeds a `terrapod_variable` of " +
					"category `ansible_machine_auth`. Byte-stable across reads of an unchanged fleet, so " +
					"it does not produce a diff of its own.",
				Computed: true,
			},
			"entries": schema.MapAttribute{
				Description: "Per host, its pinned line(s) joined by newlines and with no trailing one — " +
					"a `known_hosts` body for that host alone. Keyed as `hosts` is written. A host with " +
					"nothing pinned (unreachable on the read that would first have pinned it) is ABSENT " +
					"rather than present and empty, so `contains(keys(…), h)` answers \"is this host " +
					"pinned yet\" truthfully and a `for` over it yields only hosts that are.",
				Computed:    true,
				ElementType: types.StringType,
			},
			"unreachable_hosts": schema.SetAttribute{
				Description: "Every host this read got no key out of — which covers a host that is down " +
					"or firewalled AND a host that is up but holds none of the key types `key_types` " +
					"asked for. The two are told apart by the warning diagnostic, which carries each " +
					"attempt's error verbatim. A host listed here keeps whatever was already pinned for " +
					"it; one with nothing pinned yet is simply absent from `known_hosts` until a " +
					"later read reaches it. Named `*_hosts` rather than `unreachable` because the " +
					"Pulumi bridge pluralises a set-typed name that is not already plural, and " +
					"`unreachables` is not a word.",
				Computed:    true,
				ElementType: types.StringType,
			},
			"changed_hosts": schema.SetAttribute{
				Description: "Every host that answered with a key DIFFERING from the one pinned for the " +
					"same key type. The pin is kept and the offered key discarded, so this is a report " +
					"rather than a state change. A rebuilt host and an impersonated one look identical " +
					"from here, which is why re-pinning is a deliberate act: remove the host from `hosts` " +
					"and add it back to re-pin that host alone, or " +
					"`terraform apply -replace=…` to re-pin every host this resource manages. Named " +
					"`*_hosts` because a bare `changed` reads as a boolean, and because the Pulumi " +
					"bridge would render it `changeds`.",
				Computed:    true,
				ElementType: types.StringType,
			},
			"scanned_at": schema.StringAttribute{
				Description: "RFC3339, UTC. When this resource last ESTABLISHED or ADDED a pin — not the " +
					"time of the last scan, deliberately. Advancing it on every read would make every " +
					"plan report the resource as having changed outside Terraform, for a file whose " +
					"content had not moved, which is precisely the noise that teaches an operator to stop " +
					"reading the diff.",
				Computed: true,
			},
		},
	}
}

func (r *sshHostKeysResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan sshHostKeysModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	id, err := newLocalID()
	if err != nil {
		resp.Diagnostics.AddError("Could not generate an identifier", err.Error())
		return
	}
	plan.ID = types.StringValue(id)
	// Set unconditionally here, even when every host turned out to be
	// unreachable: the pinned set went from "no such resource" to whatever it
	// is now, which is the establishment this records.
	plan.ScannedAt = types.StringValue(nowRFC3339())

	// No prior state on a create, so every reachable host is pinned from
	// scratch -- the one read on which a scan replaces rather than adds.
	resp.Diagnostics.Append(r.readInto(ctx, &plan, nil)...)
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *sshHostKeysResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state sshHostKeysModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	// The prior pins are the whole point of the refresh: they are what a scan
	// is compared against, and what an unreachable host falls back to.
	prior, diags := priorEntries(ctx, state.Entries)
	resp.Diagnostics.Append(diags...)
	if resp.Diagnostics.HasError() {
		return
	}

	resp.Diagnostics.Append(r.readInto(ctx, &state, prior)...)
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *sshHostKeysResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan, state sshHostKeysModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	// Carried, not regenerated: the resource is the same one.
	plan.ID = state.ID
	plan.ScannedAt = state.ScannedAt

	// Prior comes from STATE while the host set comes from the PLAN, which is
	// what makes an update additive: a host that stayed keeps its pin
	// (including when it is currently unreachable), a host that was added is
	// scanned, and a host that was removed is dropped because nothing iterates
	// it any more.
	prior, diags := priorEntries(ctx, state.Entries)
	resp.Diagnostics.Append(diags...)
	if resp.Diagnostics.HasError() {
		return
	}

	resp.Diagnostics.Append(r.readInto(ctx, &plan, prior)...)
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

// Delete discards local state and nothing else. There is nothing to tear down:
// a pin is a record of what was observed, and the hosts are not this
// resource's to change.
func (r *sshHostKeysResource) Delete(_ context.Context, _ resource.DeleteRequest, _ *resource.DeleteResponse) {
}

// readInto is the one path Create, Read and Update share, so there is a single
// place where the rule about what a scan may do lives. The three differ only in
// what they pass as `prior`.
func (r *sshHostKeysResource) readInto(ctx context.Context, m *sshHostKeysModel, prior map[string][]string) diag.Diagnostics {
	var diags diag.Diagnostics

	hostEntries, d := stringsOfSet(ctx, m.Hosts)
	diags.Append(d...)
	requested, d := stringsOfList(ctx, m.KeyTypes)
	diags.Append(d...)
	rawPinned, d := stringsOfMap(ctx, m.PinnedEntries)
	diags.Append(d...)
	if diags.HasError() {
		return diags
	}

	port := defaultPort
	if !m.Port.IsNull() && !m.Port.IsUnknown() {
		port = m.Port.ValueInt64()
	}
	timeout := defaultScanTimeoutSeconds
	if !m.ScanTimeoutSeconds.IsNull() && !m.ScanTimeoutSeconds.IsUnknown() {
		timeout = m.ScanTimeoutSeconds.ValueInt64()
	}
	if timeout < 1 {
		diags.AddError("`scan_timeout_seconds` must be at least 1",
			"A zero or negative timeout is not \"no timeout\": the dialler would be given a deadline "+
				"already in the past and every host would read as unreachable.")
		return diags
	}

	hosts, d := parseHosts(hostEntries, port)
	diags.Append(d...)
	keyTypes, d := parseKeyTypes(requested)
	diags.Append(d...)
	if diags.HasError() {
		return diags
	}
	pinned, d := parsePinned(rawPinned, hosts)
	diags.Append(d...)
	if diags.HasError() {
		return diags
	}

	out, d := reconcile(ctx, aRequest{
		hosts:    hosts,
		keyTypes: keyTypes,
		timeout:  time.Duration(timeout) * time.Second,
		pinned:   pinned,
	}, prior, r.fetch)
	diags.Append(d...)
	if diags.HasError() {
		return diags
	}

	m.KnownHosts = types.StringValue(out.body)
	entries, d := types.MapValueFrom(ctx, types.StringType, joinedEntries(out.entries))
	diags.Append(d...)
	m.Entries = entries
	unreachable, d := types.SetValueFrom(ctx, types.StringType, out.unreachable)
	diags.Append(d...)
	m.UnreachableHosts = unreachable
	changed, d := types.SetValueFrom(ctx, types.StringType, out.changed)
	diags.Append(d...)
	m.ChangedHosts = changed

	// Advanced only when something was actually pinned. See the attribute's
	// description for why the alternative is worse than imprecise.
	if out.moved {
		m.ScannedAt = types.StringValue(nowRFC3339())
	}
	// A resource read before this attribute existed, or any path that somehow
	// left it unset, must not write an unknown value into state.
	if m.ScannedAt.IsNull() || m.ScannedAt.IsUnknown() {
		m.ScannedAt = types.StringValue(nowRFC3339())
	}

	return diags
}

// joinedEntries renders each host's lines as one block, which is the shape
// `entries` declares. Splitting it back on newlines recovers the lines exactly,
// because a line cannot contain one.
func joinedEntries(entries map[string][]string) map[string]string {
	out := make(map[string]string, len(entries))
	for host, lines := range entries {
		out[host] = strings.Join(lines, "\n")
	}
	return out
}

// priorEntries recovers the pinned set from state.
//
// A null map is a create or an import with nothing recorded, which is "nothing
// pinned" rather than an error.
func priorEntries(ctx context.Context, m types.Map) (map[string][]string, diag.Diagnostics) {
	blocks, diags := stringsOfMap(ctx, m)
	if diags.HasError() {
		return nil, diags
	}
	out := make(map[string][]string, len(blocks))
	for host, block := range blocks {
		if strings.TrimSpace(block) == "" {
			continue
		}
		out[host] = strings.Split(block, "\n")
	}
	return out, diags
}

func stringsOfSet(ctx context.Context, s types.Set) ([]string, diag.Diagnostics) {
	if s.IsNull() || s.IsUnknown() {
		return nil, nil
	}
	var out []string
	return out, s.ElementsAs(ctx, &out, false)
}

func stringsOfList(ctx context.Context, l types.List) ([]string, diag.Diagnostics) {
	if l.IsNull() || l.IsUnknown() {
		return nil, nil
	}
	var out []string
	return out, l.ElementsAs(ctx, &out, false)
}

func stringsOfMap(ctx context.Context, m types.Map) (map[string]string, diag.Diagnostics) {
	if m.IsNull() || m.IsUnknown() {
		return nil, nil
	}
	var out map[string]string
	return out, m.ElementsAs(ctx, &out, false)
}

// defaultKeyTypesValue is the schema default, built from the same slice the
// validator accepts against so the two cannot disagree about what the default
// is -- a default outside the accepted set would be refused on the one
// configuration that names nothing.
func defaultKeyTypesValue() types.List {
	elems := make([]attr.Value, 0, len(defaultKeyTypes))
	for _, t := range defaultKeyTypes {
		elems = append(elems, types.StringValue(t))
	}
	return types.ListValueMust(types.StringType, elems)
}

// nowRFC3339 is UTC with a trailing Z, which is the house format for every
// timestamp the provider writes.
func nowRFC3339() string {
	return time.Now().UTC().Format(time.RFC3339)
}

func newLocalID() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return "sshkeys-" + hex.EncodeToString(b[:]), nil
}
