// Package ssh_host_keys implements the terrapod_ssh_host_keys data source
// (#2592): the SSH host keys a set of hosts present, read by connecting to each
// of them.
//
// IT IS THE FIRST DATA SOURCE IN THIS PROVIDER THAT OPENS A SOCKET TO A THIRD
// PARTY. Every other one calls the Terrapod API through go-terrapod, and the
// provider's dependency surface is deliberately thin, so the departure needs a
// reason rather than a precedent. It is this: the thing being read does not
// exist anywhere else. A host key is held by the host, Terrapod has never seen
// it, and no API can be asked for it — so either the provider reads it from the
// host or an operator obtains it by hand, per host, which is the cost that makes
// a correct default into one everybody turns off -- ansible's own
// `HOST_KEY_CHECKING` being the example everyone has met. The realistic
// alternative to this data source is not out-of-band key distribution; it is
// `ssh_host_key_policy = "off"`.
//
// (This cited `#2318`'s `known_hosts` credential field as a second example until
// #2592 retired it -- tolerated on read, refused on write. The claim was true
// when written and became exactly the kind of stale reason that reads as
// considered: a reader would have gone looking for a field the API now rejects.)
//
// Two consequences of that departure, both deliberate:
//
//   - IT NEEDS NO TERRAPOD CLIENT, so it implements no Configure. A read works
//     with no token, because there is nothing to authenticate to.
//   - golang.org/x/crypto is a direct dependency of this module because of it.
//     The handshake is the whole feature and reimplementing it is out of the
//     question; v0.58.0 is the version `pulumi-provider`, `migrate` and
//     `publish` already carry, so it adds nothing new to audit. Nothing else
//     was added with it.
//
// WHERE THE SCAN RUNS IS THE CONFUSING FIRST EXPERIENCE, so it is said plainly
// in the schema as well as here. In agent mode the provider executes on the
// runner, which is the same network position the execution environment will
// later connect from — the right vantage point, and the one whose answer is
// worth anchoring trust on. In local mode it runs on whoever ran `tofu plan`,
// which may not be able to reach the hosts at all. The same configuration can
// therefore read a whole fleet from one place and nothing from the other, and
// the symptom is a pile of warnings rather than an error.
//
// IT IS A PURE READ. It scans and returns: there is no state, no pinned value,
// no comparison against a previous read and so none of the "this host's key
// changed" machinery a stateful version would need. Preserving a key across
// reads is `terrapod_inventory_host_keys`' job, because that resource already
// owns state and a key that must survive a host being briefly unreachable has
// to live somewhere that persists. Scanning is a read, so it is a data source;
// keeping is a write, so it is a resource.
//
// AND IT IS STILL TRUST-ON-FIRST-USE. What arrives is a key the responder
// demonstrably holds the private half of — x/crypto verifies the signature over
// the exchange hash before handing it over — and that is all it is. It says
// nothing about whether the responder is the host that was meant. The value is
// in what a consumer does with it afterwards: a key anchored through
// `terrapod_inventory_host_keys` is compared on every later connection, where
// verification switched off compares nothing, ever.
package ssh_host_keys

import (
	"context"
	"encoding/base64"
	"fmt"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

var _ datasource.DataSource = &sshHostKeysDataSource{}

// defaultPort and defaultTimeoutSeconds are applied in Read rather than
// declared on the schema, because a DATA SOURCE ATTRIBUTE HAS NO Default FIELD
// — the framework's `defaults` package serves resource schemas only. So the
// three optional inputs are Optional AND Computed and are written back with
// whatever was used, which is also the more useful arrangement: `port` reads
// back as 22 rather than as null, so a configuration can refer to the value it
// got without restating the default.
const (
	defaultPort           int64 = 22
	defaultTimeoutSeconds int64 = 5
)

type sshHostKeysDataSource struct{}

type sshHostKeysModel struct {
	Hosts              types.Set   `tfsdk:"hosts"`
	Port               types.Int64 `tfsdk:"port"`
	KeyTypes           types.List  `tfsdk:"key_types"`
	ScanTimeoutSeconds types.Int64 `tfsdk:"scan_timeout_seconds"`
	Entries            types.Map   `tfsdk:"entries"`
	UnreachableHosts   types.Set   `tfsdk:"unreachable_hosts"`
}

// NewDataSource returns the terrapod_ssh_host_keys data source.
func NewDataSource() datasource.DataSource {
	return &sshHostKeysDataSource{}
}

func (d *sshHostKeysDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_ssh_host_keys"
}

// hostKeyAttrTypes is the shape of one scanned key, declared once so the schema
// and the projection cannot disagree about it — a mismatch between those two is
// a runtime type error rather than a compile failure.
//
// The two fields are the platform's own identity for a host key: an
// `inventory_host_keys` row is unique on `(host_id, key_type, public_key)`, so
// the algorithm and the key are carried SEPARATELY rather than as a rendered
// `known_hosts` line. A line would have to be taken apart again by whatever
// stored it, and a host legitimately holds two keys of one type mid-rotation,
// which a map keyed by algorithm could not express either.
func hostKeyAttrTypes() map[string]attr.Type {
	return map[string]attr.Type{
		"key_type":   types.StringType,
		"public_key": types.StringType,
	}
}

func (d *sshHostKeysDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "The SSH host keys a set of hosts present, read by connecting to each one and taking " +
			"the key it offers during the key exchange — the same thing `ssh-keyscan` does, and it " +
			"authenticates nothing: the connection is abandoned before a username is sent. Exists so that " +
			"verifying host keys is cheap enough to leave on; the realistic alternative is " +
			"`ssh_host_key_policy = \"off\"`, which verifies nothing for ever. Feed `entries` into " +
			"`terrapod_inventory_host_keys` to anchor a host's keys, which is where they persist — this " +
			"data source keeps nothing between reads.\n\n" +
			"**Where it runs matters.** In agent mode the provider executes on the runner, the same place " +
			"an Ansible configure will connect from. In local mode it runs wherever you ran `plan`, which " +
			"may not reach the hosts at all — so the same configuration can read a whole fleet from one " +
			"place and nothing from the other.\n\n" +
			"**It is trust-on-first-use.** The key is verified to belong to whatever answered, not to be " +
			"the host you meant. See docs/terraform-provider.md.",
		Attributes: map[string]schema.Attribute{
			"hosts": schema.SetAttribute{
				Description: "The hosts to scan. Each entry is a hostname or address, optionally with " +
					"`:port` — which overrides `port` for that entry alone. An IPv6 literal with a port " +
					"needs the bracket form (`[2001:db8::1]:2222`), because otherwise the colons are " +
					"ambiguous; without a port it may be written either way. An entry is the key of " +
					"`entries` and the member of `unreachable_hosts` exactly as written, so a consumer " +
					"indexes with the string it configured. Two entries naming the same host and port are " +
					"refused rather than scanned twice.\n\nSupply these from the authoritative host list " +
					"— `terrapod_inventory_resolved`, narrowed by a CSP data source to the instances " +
					"actually running, since scanning an instance the cloud says is stopped can only ever " +
					"produce a warning.",
				Required:    true,
				ElementType: types.StringType,
			},
			"port": schema.Int64Attribute{
				Description: "The port to connect to for an entry of `hosts` that does not carry its own. " +
					"Defaults to 22, and reads back as the value used — a data source has no schema " +
					"default, so this is applied on read.",
				Optional: true,
				Computed: true,
			},
			"key_types": schema.ListAttribute{
				Description: "The host-key algorithms to ask each host for, one connection per algorithm — " +
					"which is necessary rather than wasteful, because a single connection offering " +
					"several lets the SERVER pick one and returns only that. Defaults to " +
					"`[\"ssh-ed25519\", \"ecdsa-sha2-nistp256\", \"rsa-sha2-512\"]`: one current algorithm " +
					"per key family a present-day sshd is likely to hold. `ssh-rsa` is deliberately not in " +
					"that default — it is RSA signed with SHA-1, which OpenSSH has disabled server-side " +
					"since 8.8, so it would fail on most of a modern fleet, and `rsa-sha2-512` reaches the " +
					"same RSA key.\n\nThese are algorithm names, not `ssh-keyscan`'s short `-t` aliases: " +
					"write `ssh-ed25519`, not `ed25519`. An unrecognised one fails the plan rather than " +
					"being skipped. Accepted: `ssh-ed25519`, `ecdsa-sha2-nistp256`, `ecdsa-sha2-nistp384`, " +
					"`ecdsa-sha2-nistp521`, `rsa-sha2-256`, `rsa-sha2-512`, `ssh-rsa`. The order is the " +
					"order a host's `entries` list comes back in.",
				Optional:    true,
				Computed:    true,
				ElementType: types.StringType,
			},
			"scan_timeout_seconds": schema.Int64Attribute{
				Description: "How long to allow ONE connection attempt, in seconds. Defaults to 5. It is " +
					"per attempt rather than per read on purpose: a budget shared across the fleet would " +
					"make each host's allowance depend on how many others were configured, so adding a " +
					"host could start timing out an unrelated one.",
				Optional: true,
				Computed: true,
			},
			"entries": schema.MapAttribute{
				Description: "Per host, the keys it presented: a map keyed on the `hosts` entry as written, " +
					"to a list of `{ key_type, public_key }`. `key_type` is the key's own FORMAT, which is " +
					"not always the algorithm asked for — `rsa-sha2-512` and `rsa-sha2-256` are signature " +
					"algorithms over one `ssh-rsa` key, so either returns a `key_type` of `ssh-rsa` and " +
					"asking for both yields one entry. `public_key` is the base64 blob alone, without the " +
					"`ssh-ed25519 ` prefix or a host field, which is the form an `inventory_host_keys` row " +
					"stores. The list is in `key_types` order.\n\n**A host that answered with nothing is " +
					"ABSENT from this map, not present with an empty list** — deliberately unlike " +
					"`terrapod_inventory_resolved`, which is exhaustive. Absence is the signal: a consumer " +
					"that looks a host up and gets null can keep whatever it already had, where an empty " +
					"list would tell it to store none.",
				Computed:    true,
				ElementType: types.ListType{ElemType: types.ObjectType{AttrTypes: hostKeyAttrTypes()}},
			},
			"unreachable_hosts": schema.SetAttribute{
				Description: "Every entry of `hosts` this read got no key out of, as written. Each one also " +
					"raises a WARNING carrying the error per algorithm attempted, which is where the " +
					"distinction lives: \"connection refused\" and \"no common algorithm for host key\" are " +
					"both \"no key\", and only the first means the host is down.\n\nIt does not fail the " +
					"plan, and that tolerance is load-bearing even with a CSP filter in front of this: " +
					"\"running\" per the cloud is not \"reachable from here\" — a security group, an " +
					"sshd that has not started, an instance still in cloud-init. Failing would let one " +
					"booting fleet member block every other host's keys from being read. Gate on this " +
					"attribute if a missing key should stop something.",
				Computed:    true,
				ElementType: types.StringType,
			},
		},
	}
}

func (d *sshHostKeysDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config sshHostKeysModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	request, diags := requestFromConfig(ctx, &config)
	resp.Diagnostics.Append(diags...)
	if resp.Diagnostics.HasError() {
		return
	}

	results := scanAll(ctx, request.hosts, request.keyTypes, request.timeout, dialForHostKeys)

	resp.Diagnostics.Append(readIntoModel(request.hosts, results, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &config)...)
}

// requestFromConfig unwraps and validates the inputs, and writes the resolved
// defaults back into the model.
//
// The write-back is why this takes the model rather than returning only the
// request: `port`, `key_types` and `scan_timeout_seconds` are Computed, so the
// framework requires a known value for each after a read, and the value it
// should record is the one that was actually used.
func requestFromConfig(ctx context.Context, m *sshHostKeysModel) (aRequest, diag.Diagnostics) {
	var diags diag.Diagnostics
	var out aRequest

	var rawHosts []string
	diags.Append(m.Hosts.ElementsAs(ctx, &rawHosts, false)...)

	port := defaultPort
	if !m.Port.IsNull() && !m.Port.IsUnknown() {
		port = m.Port.ValueInt64()
	}
	m.Port = types.Int64Value(port)

	seconds := defaultTimeoutSeconds
	if !m.ScanTimeoutSeconds.IsNull() && !m.ScanTimeoutSeconds.IsUnknown() {
		seconds = m.ScanTimeoutSeconds.ValueInt64()
	}
	m.ScanTimeoutSeconds = types.Int64Value(seconds)

	var rawKeyTypes []string
	if !m.KeyTypes.IsNull() && !m.KeyTypes.IsUnknown() {
		diags.Append(m.KeyTypes.ElementsAs(ctx, &rawKeyTypes, false)...)
	}
	if diags.HasError() {
		return out, diags
	}

	// The port is validated BEFORE the hosts, because parseHosts applies it to
	// every entry that carries no port of its own -- so an out-of-range `port`
	// would otherwise be reported once per host, as a complaint about each
	// host's spelling rather than about the one value that is wrong.
	if port < 1 || port > 65535 {
		diags.AddError("`port` is outside 1-65535",
			fmt.Sprintf("Got %d. It is the default port for entries of `hosts` that do not name one.", port))
		return out, diags
	}

	timeout, d := parseTimeout(seconds)
	diags.Append(d...)
	keyTypes, d := parseKeyTypes(rawKeyTypes)
	diags.Append(d...)
	hosts, d := parseHosts(rawHosts, port)
	diags.Append(d...)
	if diags.HasError() {
		return out, diags
	}

	resolved, d := types.ListValueFrom(ctx, types.StringType, keyTypes)
	diags.Append(d...)
	m.KeyTypes = resolved

	out = aRequest{hosts: hosts, keyTypes: keyTypes, timeout: timeout}
	return out, diags
}

// readIntoModel projects the scan into the model, and warns about every host
// that produced nothing.
//
// It iterates the SORTED target list rather than the results map, so the
// warnings come out in the same order on every read. A map iteration would
// reorder them between two reads of an unchanged configuration, which reads to
// anyone diffing plan output as something having changed.
func readIntoModel(targets []aHost, results map[string]scanResult, m *sshHostKeysModel) diag.Diagnostics {
	var diags diag.Diagnostics

	keyType := types.ObjectType{AttrTypes: hostKeyAttrTypes()}
	listType := types.ListType{ElemType: keyType}

	entries := make(map[string]attr.Value, len(targets))
	unreachable := make([]attr.Value, 0, len(targets))

	for _, t := range targets {
		res := results[t.key]
		if len(res.keys) == 0 {
			unreachable = append(unreachable, types.StringValue(t.key))
			diags.AddWarning(fmt.Sprintf("No SSH host key read from %q", t.key), unreachableDetail(t, res))
			// Absent from `entries` rather than present and empty -- see the
			// attribute's own description. A consumer reading null can keep
			// what it has; an empty list would tell it to store none.
			continue
		}

		rows := make([]attr.Value, 0, len(res.keys))
		for _, k := range res.keys {
			obj, d := types.ObjectValue(hostKeyAttrTypes(), map[string]attr.Value{
				"key_type": types.StringValue(k.Type()),
				// base64 of the wire form, which is exactly the middle field of
				// an authorized_keys/known_hosts line -- pinned by a test
				// against ssh.MarshalAuthorizedKey rather than asserted here.
				"public_key": types.StringValue(base64.StdEncoding.EncodeToString(k.Marshal())),
			})
			diags.Append(d...)
			rows = append(rows, obj)
		}
		list, d := types.ListValue(keyType, rows)
		diags.Append(d...)
		entries[t.key] = list
	}

	entriesValue, d := types.MapValue(listType, entries)
	diags.Append(d...)
	m.Entries = entriesValue

	// Already sorted, because `targets` is. Sorted again would be a no-op; the
	// invariant is worth stating rather than relying on, so the test asserts
	// the attribute rather than the slice.
	unreachableValue, d := types.SetValue(types.StringType, unreachable)
	diags.Append(d...)
	m.UnreachableHosts = unreachableValue

	return diags
}

// unreachableDetail is the body of the per-host warning.
//
// One warning PER HOST rather than one summarising all of them: each is
// separately addressable, an operator can copy a single host's error into a
// search, and when a whole fleet is down the count is itself the signal. A
// single diagnostic listing three hundred hosts is a wall.
func unreachableDetail(t aHost, res scanResult) string {
	var b strings.Builder
	fmt.Fprintf(&b, "Dialled %s and got no host key. This does not fail the plan — %q is in "+
		"`unreachable_hosts` and absent from `entries`, so a consumer can keep whatever it already "+
		"holds for it.\n\n", t.address, t.key)
	if len(res.errs) == 0 {
		// Reachable in principle: no algorithm was asked for. parseKeyTypes
		// cannot produce an empty list, so this is a guard against a future
		// caller rather than a state a configuration can reach.
		b.WriteString("No algorithm was attempted.")
		return b.String()
	}
	b.WriteString("Per algorithm attempted:\n")
	for _, e := range res.errs {
		b.WriteString("  - " + e + "\n")
	}
	b.WriteString("\nA connection error means the host is unreachable from wherever this provider ran — " +
		"which in agent mode is the runner and in local mode is your own machine. An error about a " +
		"host-key algorithm means the host answered and holds none of the types `key_types` asked for.")
	return b.String()
}
