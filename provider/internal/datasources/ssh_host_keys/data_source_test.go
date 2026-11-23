package ssh_host_keys

import (
	"context"
	"encoding/base64"
	"fmt"
	"net"
	"slices"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/tfsdk"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-go/tftypes"
	"golang.org/x/crypto/ssh"
)

// Read is driven END TO END against the real in-process sshd from scan_test.go
// rather than against a substituted fetcher, so no seam is opened for the
// tests' benefit and the call site is covered along with the decision: a Read
// that unwrapped the config perfectly and then scanned the wrong thing would
// fail here, where testing the helpers alone it would not.

func TestReadReturnsEachHostsKeys(t *testing.T) {
	ed, ec := ed25519Signer(t), ecdsaSigner(t)
	addr := startSSHServer(t, ed, ec)

	state, diags := read(t, configWith(t, hosts(addr), nil, nil, nil))
	if diags.HasError() {
		t.Fatalf("read: %s", diagText(diags.Errors()))
	}
	if len(diags.Warnings()) > 0 {
		t.Fatalf("unexpected warnings: %s", diagText(diags.Warnings()))
	}

	got := entriesOf(t, state)
	if len(got) != 1 {
		t.Fatalf("entries has %d hosts, want 1: %v", len(got), got)
	}
	// Keyed on the entry AS WRITTEN, which is what lets a consumer look a host
	// up with the string it configured.
	rows, ok := got[addr]
	if !ok {
		t.Fatalf("entries is not keyed on the configured entry %q: %v", addr, got)
	}
	// Both algorithms the default asks for and this fixture holds, in the
	// default's order (ed25519 before ecdsa-sha2-nistp256).
	want := []scannedKey{
		{keyType: ssh.KeyAlgoED25519, publicKey: blobOf(ed)},
		{keyType: ssh.KeyAlgoECDSA256, publicKey: blobOf(ec)},
	}
	if !slices.Equal(rows, want) {
		t.Errorf("entries[%q] = %v, want %v", addr, rows, want)
	}

	if u := unreachableOf(t, state); len(u) != 0 {
		t.Errorf("unreachable_hosts = %v, want none", u)
	}
}

// THE TOLERANCE, which is the whole reason this is a warning: one host that
// cannot be reached must not stop the others' keys being read. If it failed the
// plan, a single fleet member still in cloud-init would block every other
// host's key -- and the hosts come from a CSP filter that says "running", which
// is not the same as "reachable from here".
func TestAnUnreachableHostWarnsAndLeavesTheOthersIntact(t *testing.T) {
	addr := startSSHServer(t, ed25519Signer(t))
	dead := closedPort(t)

	state, diags := read(t, configWith(t, hosts(addr, dead), nil, nil, nil))
	if diags.HasError() {
		t.Fatalf("an unreachable host must not fail the plan: %s", diagText(diags.Errors()))
	}

	// The reachable host still answered. This is the assertion that would fail
	// if the scan abandoned the batch on the first failure.
	got := entriesOf(t, state)
	if _, ok := got[addr]; !ok {
		t.Errorf("the reachable host is missing from entries: %v", got)
	}
	// ABSENT from entries, not present with an empty list -- see the
	// attribute's description. Absence is what lets a consumer keep whatever it
	// already holds; an empty list would tell it to store none.
	if rows, ok := got[dead]; ok {
		t.Errorf("the unreachable host is present in entries as %v; it must be absent", rows)
	}
	if u := unreachableOf(t, state); !slices.Equal(u, []string{dead}) {
		t.Errorf("unreachable_hosts = %v, want [%q]", u, dead)
	}

	// One warning, naming the host, carrying the per-algorithm error verbatim.
	warnings := diags.Warnings()
	if len(warnings) != 1 {
		t.Fatalf("got %d warnings, want 1: %s", len(warnings), diagText(warnings))
	}
	if !strings.Contains(warnings[0].Summary(), dead) {
		t.Errorf("the warning does not name the host: %q", warnings[0].Summary())
	}
	for _, algo := range defaultKeyTypes {
		if !strings.Contains(warnings[0].Detail(), algo) {
			t.Errorf("the warning does not report the attempt for %q: %q", algo, warnings[0].Detail())
		}
	}
	if !strings.Contains(warnings[0].Detail(), "unreachable_hosts") {
		t.Errorf("the warning should say where the host was reported: %q", warnings[0].Detail())
	}
}

// A host that is UP and holds none of the requested types is reported the same
// way as one that is down -- deliberately, because both mean "this read got no
// key" -- and the warning's text is the only thing that tells them apart.
func TestAHostHoldingNoneOfTheRequestedTypesWarnsWithTheAlgorithm(t *testing.T) {
	addr := startSSHServer(t, ed25519Signer(t))

	state, diags := read(t, configWith(t, hosts(addr), nil, []string{ssh.KeyAlgoECDSA384}, nil))
	if diags.HasError() {
		t.Fatalf("read: %s", diagText(diags.Errors()))
	}
	if got := entriesOf(t, state); len(got) != 0 {
		t.Errorf("entries = %v, want none", got)
	}
	if u := unreachableOf(t, state); !slices.Equal(u, []string{addr}) {
		t.Errorf("unreachable_hosts = %v, want [%q]", u, addr)
	}
	warnings := diags.Warnings()
	if len(warnings) != 1 {
		t.Fatalf("got %d warnings, want 1: %s", len(warnings), diagText(warnings))
	}
	if !strings.Contains(warnings[0].Detail(), ssh.KeyAlgoECDSA384) {
		t.Errorf("the warning does not name the algorithm asked for: %q", warnings[0].Detail())
	}
	// And it says which of the two cases this is, since the attribute cannot.
	if !strings.Contains(warnings[0].Detail(), "host-key algorithm") {
		t.Errorf("the warning should distinguish an algorithm mismatch from an unreachable host: %q",
			warnings[0].Detail())
	}
}

// Two reads of an unchanged configuration must agree, and the part that could
// drift is ORDER: `hosts` is a set, so without the sort in parseHosts the
// warnings would come out in map-iteration order and reorder between reads --
// which reads to anyone diffing plan output as something having changed.
func TestTwoReadsOfOneConfigurationAgree(t *testing.T) {
	addr := startSSHServer(t, ed25519Signer(t), ecdsaSigner(t))
	// Deliberately given in reverse order, and more unreachable hosts than one,
	// so the sort has something to do.
	cfg := func() tfsdk.Config {
		return configWith(t, hosts("zzz.invalid", addr, "mmm.invalid", "aaa.invalid"), nil, nil, int64Ptr(1))
	}

	first, firstDiags := read(t, cfg())
	second, secondDiags := read(t, cfg())
	if firstDiags.HasError() || secondDiags.HasError() {
		t.Fatalf("read: %s / %s", diagText(firstDiags.Errors()), diagText(secondDiags.Errors()))
	}

	if a, b := entriesOf(t, first), entriesOf(t, second); fmt.Sprint(a) != fmt.Sprint(b) {
		t.Errorf("entries differ between two reads:\n  %v\n  %v", a, b)
	}
	if a, b := unreachableOf(t, first), unreachableOf(t, second); !slices.Equal(a, b) {
		t.Errorf("unreachable_hosts differ between two reads: %v vs %v", a, b)
	}

	// The warnings are in the same order, and that order is sorted by host.
	var names [][]string
	for _, d := range []diag.Diagnostics{firstDiags, secondDiags} {
		var got []string
		for _, w := range d.Warnings() {
			got = append(got, w.Summary())
		}
		names = append(names, got)
	}
	if !slices.Equal(names[0], names[1]) {
		t.Errorf("the warnings reordered between two reads:\n  %v\n  %v", names[0], names[1])
	}
	if !slices.IsSortedFunc(names[0], strings.Compare) {
		t.Errorf("the warnings are not in a stable order: %v", names[0])
	}
}

// A data source attribute has no Default field, so each default is applied in
// Read -- and because the three inputs are Computed, the framework requires a
// known value for each afterwards. Writing back what was USED is what makes
// `port` read as 22 rather than as null.
func TestTheDefaultsAreWrittenBack(t *testing.T) {
	addr := startSSHServer(t, ed25519Signer(t))

	state, diags := read(t, configWith(t, hosts(addr), nil, nil, nil))
	if diags.HasError() {
		t.Fatalf("read: %s", diagText(diags.Errors()))
	}
	var m sshHostKeysModel
	if d := state.Get(context.Background(), &m); d.HasError() {
		t.Fatalf("reading the state back: %s", diagText(d.Errors()))
	}
	if m.Port.ValueInt64() != defaultPort {
		t.Errorf("port = %v, want %d", m.Port, defaultPort)
	}
	if m.ScanTimeoutSeconds.ValueInt64() != defaultTimeoutSeconds {
		t.Errorf("scan_timeout_seconds = %v, want %d", m.ScanTimeoutSeconds, defaultTimeoutSeconds)
	}
	var gotTypes []string
	if d := m.KeyTypes.ElementsAs(context.Background(), &gotTypes, false); d.HasError() {
		t.Fatalf("key_types: %s", diagText(d.Errors()))
	}
	if !slices.Equal(gotTypes, defaultKeyTypes) {
		t.Errorf("key_types = %v, want %v", gotTypes, defaultKeyTypes)
	}
}

// An explicit `port` is the default only for entries that do not carry one, and
// a per-entry port wins. Driven through Read because that is where the two are
// combined.
func TestAPerEntryPortOverridesThePortAttribute(t *testing.T) {
	addr := startSSHServer(t, ed25519Signer(t))
	host, port, err := net.SplitHostPort(addr)
	if err != nil {
		t.Fatalf("splitting the fixture's address: %v", err)
	}

	// `port` names a port nothing is on; the entry names the real one, in the
	// bracket form an operator copying from an ssh config would write.
	bracketed := "[" + host + "]:" + port
	state, diags := read(t, configWith(t, hosts(bracketed), int64Ptr(1), nil, int64Ptr(1)))
	if diags.HasError() {
		t.Fatalf("read: %s", diagText(diags.Errors()))
	}
	got := entriesOf(t, state)
	if _, ok := got[bracketed]; !ok {
		t.Fatalf("the per-entry port did not win, or the bracket form was not keyed as written: %v", got)
	}
	if u := unreachableOf(t, state); len(u) != 0 {
		t.Errorf("unreachable_hosts = %v, want none", u)
	}
}

func TestReadRefusesAConfigurationMistake(t *testing.T) {
	addr := startSSHServer(t, ed25519Signer(t))

	for _, tc := range []struct {
		name    string
		cfg     func(t *testing.T) tfsdk.Config
		wantErr string
	}{
		{
			name: "an unknown key type",
			cfg: func(t *testing.T) tfsdk.Config {
				return configWith(t, hosts(addr), nil, []string{"ed25519"}, nil)
			},
			wantErr: `Unknown host-key algorithm "ed25519"`,
		},
		{
			name: "two entries naming one host",
			cfg: func(t *testing.T) tfsdk.Config {
				host, port, _ := net.SplitHostPort(addr)
				return configWith(t, hosts(addr, "["+host+"]:"+port), nil, nil, nil)
			},
			wantErr: "name the same host",
		},
		{
			name: "a malformed entry",
			cfg: func(t *testing.T) tfsdk.Config {
				return configWith(t, hosts("web1:ssh"), nil, nil, nil)
			},
			wantErr: "Malformed host",
		},
		{
			name: "a timeout of zero",
			cfg: func(t *testing.T) tfsdk.Config {
				return configWith(t, hosts(addr), nil, nil, int64Ptr(0))
			},
			wantErr: "must be at least 1",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			state, diags := read(t, tc.cfg(t))
			if !diags.HasError() {
				t.Fatalf("want an error containing %q, got none", tc.wantErr)
			}
			if !strings.Contains(diagText(diags.Errors()), tc.wantErr) {
				t.Errorf("errors = %q, want one containing %q", diagText(diags.Errors()), tc.wantErr)
			}
			// Nothing is written: the state stays null, so a failed read cannot
			// leave a half-filled answer behind.
			if !state.Raw.IsNull() {
				t.Errorf("a refused read wrote state: %s", state.Raw)
			}
		})
	}
}

// An out-of-range `port` is reported ONCE, as a complaint about the one value
// that is wrong -- not once per host, which is what reporting it from inside
// parseHosts would produce.
func TestAnOutOfRangePortIsReportedOnceAndNotPerHost(t *testing.T) {
	_, diags := read(t, configWith(t, hosts("web1", "web2", "web3"), int64Ptr(70000), nil, nil))
	if !diags.HasError() {
		t.Fatal("an out-of-range port must fail the plan")
	}
	if n := len(diags.Errors()); n != 1 {
		t.Errorf("got %d errors, want 1: %s", n, diagText(diags.Errors()))
	}
	if !strings.Contains(diagText(diags.Errors()), "`port` is outside 1-65535") {
		t.Errorf("errors = %q, want the complaint to be about `port`", diagText(diags.Errors()))
	}
}

// An empty `hosts` is legitimate -- it is what a `for_each` over an empty set
// produces -- and must give known, empty collections rather than null ones, or
// a configuration indexing into `entries` errors about a null value instead of
// finding no such key.
func TestAnEmptyHostSetIsNotAnError(t *testing.T) {
	state, diags := read(t, configWith(t, hosts(), nil, nil, nil))
	if diags.HasError() {
		t.Fatalf("an empty host set must not fail: %s", diagText(diags.Errors()))
	}
	var m sshHostKeysModel
	if d := state.Get(context.Background(), &m); d.HasError() {
		t.Fatalf("reading the state back: %s", diagText(d.Errors()))
	}
	if m.Entries.IsNull() || m.UnreachableHosts.IsNull() {
		t.Errorf("entries=%v unreachable_hosts=%v; both must be empty rather than null",
			m.Entries, m.UnreachableHosts)
	}
	if len(m.Entries.Elements()) != 0 || len(m.UnreachableHosts.Elements()) != 0 {
		t.Errorf("entries=%v unreachable_hosts=%v; want both empty", m.Entries, m.UnreachableHosts)
	}
}

// THE SCHEMA ITSELF, because two of its properties are load-bearing and neither
// is visible in a behavioural test: `hosts` must be Required (a null set would
// reach ElementsAs), and the three inputs must be Computed or the framework
// refuses the write-back of their defaults.
func TestTheSchemaShape(t *testing.T) {
	resp, _ := schemaOf(t)
	attrs := resp.Schema.Attributes

	if a := attrs["hosts"]; !a.IsRequired() {
		t.Error("`hosts` must be Required")
	}
	for _, name := range []string{"port", "key_types", "scan_timeout_seconds"} {
		a := attrs[name]
		if !a.IsOptional() || !a.IsComputed() {
			t.Errorf("`%s` must be Optional AND Computed: optional=%v computed=%v",
				name, a.IsOptional(), a.IsComputed())
		}
	}
	for _, name := range []string{"entries", "unreachable_hosts"} {
		a := attrs[name]
		if !a.IsComputed() || a.IsOptional() || a.IsRequired() {
			t.Errorf("`%s` must be Computed only", name)
		}
	}
	// A host key is public, and marking it sensitive would hide the one value
	// an operator needs to read when a key does not match -- the same reasoning
	// the platform records for not registering these with the log redactor.
	if attrs["entries"].IsSensitive() {
		t.Error("`entries` must not be sensitive: a host key is public, and hiding it would obscure the " +
			"one value needed to diagnose a mismatch")
	}
	// Every attribute carries a description: this is the provider's first data
	// source that talks to something other than the Terrapod API, so what it
	// does and where it runs cannot be inferred from its neighbours.
	for name, a := range attrs {
		if strings.TrimSpace(a.GetDescription()) == "" {
			t.Errorf("`%s` has no description", name)
		}
	}
	if strings.TrimSpace(resp.Schema.Description) == "" {
		t.Error("the data source has no description")
	}
}

// --- helpers ---

type scannedKey struct {
	keyType   string
	publicKey string
}

func (k scannedKey) String() string { return k.keyType + " " + k.publicKey[:12] + "…" }

func blobOf(s ssh.Signer) string {
	return base64.StdEncoding.EncodeToString(s.PublicKey().Marshal())
}

func hosts(entries ...string) []string { return entries }

func int64Ptr(v int64) *int64 { return &v }

// closedPort returns an address nothing is listening on: a listener opened and
// immediately closed.
func closedPort(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	addr := ln.Addr().String()
	_ = ln.Close()
	return addr
}

func read(t *testing.T, cfg tfsdk.Config) (tfsdk.State, diag.Diagnostics) {
	t.Helper()
	resp := datasource.ReadResponse{State: emptyState(t)}
	NewDataSource().Read(context.Background(), datasource.ReadRequest{Config: cfg}, &resp)
	return resp.State, resp.Diagnostics
}

func schemaOf(t *testing.T) (datasource.SchemaResponse, tftypes.Object) {
	t.Helper()
	ctx := context.Background()
	var resp datasource.SchemaResponse
	NewDataSource().Schema(ctx, datasource.SchemaRequest{}, &resp)
	objType, ok := resp.Schema.Type().TerraformType(ctx).(tftypes.Object)
	if !ok {
		t.Fatal("the schema's terraform type is not an object")
	}
	return resp, objType
}

// emptyState is a NULL state, so a test can tell "nothing was written" apart
// from "a zero-valued answer was written".
func emptyState(t *testing.T) tfsdk.State {
	t.Helper()
	resp, objType := schemaOf(t)
	return tfsdk.State{Schema: resp.Schema, Raw: tftypes.NewValue(objType, nil)}
}

// configWith builds the config a practitioner's block would produce: the
// attributes they can write, and null for everything else.
//
// tfsdk.Config has no Set, so the raw value is assembled by hand. Leaving the
// computed attributes null is also what keeps this honest -- one arriving
// pre-filled from the config would let Read pass while never having scanned.
func configWith(t *testing.T, hostEntries []string, port *int64, keyTypes []string, timeout *int64) tfsdk.Config {
	t.Helper()
	resp, objType := schemaOf(t)
	attrs := make(map[string]tftypes.Value, len(objType.AttributeTypes))
	for name, ty := range objType.AttributeTypes {
		attrs[name] = tftypes.NewValue(ty, nil)
	}

	members := make([]tftypes.Value, 0, len(hostEntries))
	for _, h := range hostEntries {
		members = append(members, tftypes.NewValue(tftypes.String, h))
	}
	attrs["hosts"] = tftypes.NewValue(tftypes.Set{ElementType: tftypes.String}, members)

	if keyTypes != nil {
		elems := make([]tftypes.Value, 0, len(keyTypes))
		for _, k := range keyTypes {
			elems = append(elems, tftypes.NewValue(tftypes.String, k))
		}
		attrs["key_types"] = tftypes.NewValue(tftypes.List{ElementType: tftypes.String}, elems)
	}
	if port != nil {
		attrs["port"] = tftypes.NewValue(tftypes.Number, *port)
	}
	if timeout != nil {
		attrs["scan_timeout_seconds"] = tftypes.NewValue(tftypes.Number, *timeout)
	}
	return tfsdk.Config{Schema: resp.Schema, Raw: tftypes.NewValue(objType, attrs)}
}

// entriesOf projects `entries` back out of state into plain Go, so a test
// asserts on the keys and the order rather than on framework types.
func entriesOf(t *testing.T, state tfsdk.State) map[string][]scannedKey {
	t.Helper()
	var m sshHostKeysModel
	if d := state.Get(context.Background(), &m); d.HasError() {
		t.Fatalf("reading the state back: %s", diagText(d.Errors()))
	}
	out := map[string][]scannedKey{}
	for host, raw := range m.Entries.Elements() {
		list, ok := raw.(types.List)
		if !ok {
			t.Fatalf("entries[%q] is %T, want a list", host, raw)
		}
		var rows []scannedKey
		for _, e := range list.Elements() {
			obj, ok := e.(types.Object)
			if !ok {
				t.Fatalf("entries[%q] holds a %T, want an object", host, e)
			}
			at := obj.Attributes()
			rows = append(rows, scannedKey{
				keyType:   at["key_type"].(types.String).ValueString(),
				publicKey: at["public_key"].(types.String).ValueString(),
			})
		}
		out[host] = rows
	}
	return out
}

// unreachableOf returns `unreachable_hosts`, sorted -- a Terraform set carries
// no order, so sorting here is what makes an equality assertion meaningful
// without pretending the attribute is ordered.
func unreachableOf(t *testing.T, state tfsdk.State) []string {
	t.Helper()
	var m sshHostKeysModel
	if d := state.Get(context.Background(), &m); d.HasError() {
		t.Fatalf("reading the state back: %s", diagText(d.Errors()))
	}
	var out []string
	for _, e := range m.UnreachableHosts.Elements() {
		out = append(out, e.(types.String).ValueString())
	}
	slices.Sort(out)
	return out
}

func diagText[T diag.Diagnostic](ds []T) string {
	var parts []string
	for _, d := range ds {
		parts = append(parts, d.Summary()+": "+d.Detail())
	}
	return strings.Join(parts, " | ")
}
