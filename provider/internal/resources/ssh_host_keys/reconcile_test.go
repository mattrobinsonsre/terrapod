package ssh_host_keys

import (
	"context"
	"strings"
	"testing"
	"time"

	"golang.org/x/crypto/ssh"
	"golang.org/x/crypto/ssh/knownhosts"
)

// fakeFleet is a fetcher backed by a table of what each ADDRESS answered.
//
// It ignores the requested algorithms on purpose. Filtering a host's keys by
// what was asked for is the real fetcher's job -- one connection per algorithm,
// pinned against a real sshd in scan_test.go -- so modelling it again here
// would be asserting my own implementation twice. What this fake models is the
// only thing reconcile cares about: what came back.
type fakeFleet map[string]scanResult

func (f fakeFleet) fetch(_ context.Context, address string, _ []string, _ time.Duration) scanResult {
	res, known := f[address]
	if !known {
		// An address the table says nothing about is a host that is not there.
		return scanResult{errs: []string{"dial tcp " + address + ": connect: connection refused"}}
	}
	return res
}

func answered(keys ...ssh.PublicKey) scanResult { return scanResult{keys: keys} }

func unreachable(why string) scanResult { return scanResult{errs: []string{why}} }

// lineFor is what the resource will render for one host and key, so a test's
// expectation is built the same way the code builds it rather than by hand.
func lineFor(address string, key ssh.PublicKey) string {
	return strings.TrimRight(knownhosts.Line([]string{address}, key), "\n")
}

func target(key string) aHost { return aHost{key: key, address: key + ":22"} }

func req(hosts ...aHost) aRequest {
	return aRequest{hosts: hosts, keyTypes: defaultKeyTypes, timeout: time.Second}
}

func run(t *testing.T, r aRequest, prior map[string][]string, fleet fakeFleet) anOutcome {
	t.Helper()
	out, diags := reconcile(context.Background(), r, prior, fleet.fetch)
	if diags.HasError() {
		t.Fatalf("unexpected error diagnostics: %v", diags.Errors())
	}
	return out
}

// ── A clean scan ─────────────────────────────────────────────────────────────

func TestACleanScanPinsEveryHost(t *testing.T) {
	a, b := ed25519Signer(t).PublicKey(), ecdsaSigner(t).PublicKey()
	fleet := fakeFleet{
		"web1:22": answered(a),
		"web2:22": answered(b),
	}

	out := run(t, req(target("web1"), target("web2")), nil, fleet)

	if len(out.unreachable) != 0 || len(out.changed) != 0 {
		t.Fatalf("unreachable=%v changed=%v, want neither", out.unreachable, out.changed)
	}
	if !out.moved {
		t.Error("a create that pinned two hosts must advance scanned_at")
	}
	want := lineFor("web1:22", a) + "\n" + lineFor("web2:22", b) + "\n"
	if out.body != want {
		t.Errorf("body =\n%q\nwant\n%q", out.body, want)
	}
	if got := out.entries["web1"]; len(got) != 1 || got[0] != lineFor("web1:22", a) {
		t.Errorf("entries[web1] = %v", got)
	}
}

// Hosts are sorted in the body, and each host's lines keep the order they were
// pinned in. Without the sort the body is a Go map's iteration order, which is
// randomised -- so every plan would show the downstream variable changing for a
// file whose content had not moved.
func TestTheBodyIsByteStableAcrossTwoReads(t *testing.T) {
	keys := []ssh.PublicKey{
		ed25519Signer(t).PublicKey(), ecdsaSigner(t).PublicKey(),
		ed25519Signer(t).PublicKey(), ecdsaSigner(t).PublicKey(),
		ed25519Signer(t).PublicKey(), ecdsaSigner(t).PublicKey(),
	}
	names := []string{"zeta", "alpha", "mu", "beta", "omega", "kappa"}
	fleet := fakeFleet{}
	var hosts []aHost
	for i, n := range names {
		hosts = append(hosts, target(n))
		fleet[n+":22"] = answered(keys[i])
	}

	first := run(t, req(hosts...), nil, fleet)

	// Re-read with the first read's pins as prior, which is what a refresh
	// does. Nothing may move.
	second := run(t, req(hosts...), first.entries, fleet)

	if second.body != first.body {
		t.Errorf("a re-read of an unchanged fleet moved the body:\nfirst:\n%s\nsecond:\n%s",
			first.body, second.body)
	}
	if second.moved {
		t.Error("a re-read that pinned nothing new must not advance scanned_at")
	}
	if !sortedAscending(strings.Split(strings.TrimRight(first.body, "\n"), "\n")) {
		t.Errorf("the body is not in sorted host order:\n%s", first.body)
	}
	// And repeating the derivation many times must not vary: a single pass
	// could get lucky on map order.
	for range 50 {
		again := run(t, req(hosts...), first.entries, fleet)
		if again.body != first.body {
			t.Fatalf("the body is not stable across repeated reads")
		}
	}
}

// ── pinned_entries wins ──────────────────────────────────────────────────────

func TestAPinnedEntryWinsAndTheHostIsNeverScanned(t *testing.T) {
	scanned := ed25519Signer(t).PublicKey()
	declared := lineFor("web1:22", ecdsaSigner(t).PublicKey())

	var dialled []string
	fleet := fakeFleet{"web1:22": answered(scanned)}
	fetch := func(ctx context.Context, address string, kt []string, d time.Duration) scanResult {
		dialled = append(dialled, address)
		return fleet.fetch(ctx, address, kt, d)
	}

	r := req(target("web1"))
	r.pinned = map[string][]string{"web1": {declared}}
	out, diags := reconcile(context.Background(), r, nil, fetch)
	if diags.HasError() {
		t.Fatalf("errors: %v", diags.Errors())
	}

	if len(dialled) != 0 {
		t.Errorf("a declared host was dialled anyway: %v", dialled)
	}
	if out.body != declared+"\n" {
		t.Errorf("body = %q, want the declared line %q", out.body, declared)
	}
	if len(out.changed) != 0 {
		t.Errorf("changed = %v; a declared host cannot have changed, nothing scanned it", out.changed)
	}
}

// A value may hold several lines, for a host offering several key types.
func TestAPinnedEntryMayHoldSeveralLines(t *testing.T) {
	one := lineFor("web1:22", ed25519Signer(t).PublicKey())
	two := lineFor("web1:22", ecdsaSigner(t).PublicKey())

	lines, err := parseKnownHostsBlock("# the fleet's keys\n" + one + "\n\n" + two + "\n")
	if err != nil {
		t.Fatalf("parsing a two-line block: %v", err)
	}
	if len(lines) != 2 || lines[0] != one || lines[1] != two {
		t.Fatalf("lines = %v", lines)
	}
}

func TestAPinnedEntryThatIsNotAKnownHostsLineIsAnError(t *testing.T) {
	for _, bad := range []string{
		"web1.example.com",
		"web1.example.com ssh-ed25519 not-base64!!",
		"# only a comment",
		"",
		"   \n\n  ",
	} {
		t.Run(bad, func(t *testing.T) {
			if _, err := parseKnownHostsBlock(bad); err == nil {
				t.Errorf("%q was accepted as a known_hosts entry", bad)
			}
		})
	}
}

// An ORPHAN pin -- one naming a host that is not in `hosts` -- is a warning and
// not an error, and the asymmetry with the refusals above is deliberate:
// `hosts` is routinely derived from an inventory, so a host legitimately
// leaving the fleet must not start failing every apply.
func TestAPinForAHostNotInHostsWarnsRatherThanFailing(t *testing.T) {
	line := lineFor("web9:22", ed25519Signer(t).PublicKey())
	hosts := []aHost{target("web1")}

	pinned, diags := parsePinned(map[string]string{"web9": line}, hosts)
	if diags.HasError() {
		t.Fatalf("an orphan pin must not be an error: %v", diags.Errors())
	}
	if len(diags.Warnings()) != 1 {
		t.Fatalf("got %d warnings, want 1", len(diags.Warnings()))
	}
	if !strings.Contains(diags.Warnings()[0].Detail(), "web9") {
		t.Errorf("the warning does not name the orphan: %q", diags.Warnings()[0].Detail())
	}
	// It is still parsed, so adding the host to `hosts` is all that is needed.
	if _, ok := pinned["web9"]; !ok {
		t.Error("the orphan pin was discarded rather than kept")
	}
}

// ── Unreachable ──────────────────────────────────────────────────────────────

// At create there is nothing to fall back to, so the host is simply absent from
// the file -- not an error, and not an empty entry either, so a configuration
// can tell "nothing pinned" from "pinned to nothing".
func TestAnUnreachableHostAtCreateIsOmittedAndListed(t *testing.T) {
	alive := ed25519Signer(t).PublicKey()
	fleet := fakeFleet{
		"web1:22": answered(alive),
		"web2:22": unreachable("dial tcp web2:22: i/o timeout"),
	}

	out, diags := reconcile(context.Background(), req(target("web1"), target("web2")), nil, fleet.fetch)
	if diags.HasError() {
		t.Fatalf("an unreachable host must not be an error: %v", diags.Errors())
	}
	if len(diags.Warnings()) != 1 {
		t.Fatalf("got %d warnings, want 1", len(diags.Warnings()))
	}
	w := diags.Warnings()[0]
	if !strings.Contains(w.Detail(), "web2") || !strings.Contains(w.Detail(), "i/o timeout") {
		t.Errorf("the warning must name the host and carry the error verbatim: %q", w.Detail())
	}
	// The summary counts HOSTS, and a detail line is per attempt -- so an
	// algorithm that failed three ways on one host must still read "1 host".
	if got := w.Summary(); got != "1 host could not be reached" {
		t.Errorf("summary = %q", got)
	}

	if got := out.unreachable; len(got) != 1 || got[0] != "web2" {
		t.Errorf("unreachable = %v, want [web2]", got)
	}
	if _, present := out.entries["web2"]; present {
		t.Error("a host with nothing pinned must be ABSENT from entries, not present and empty")
	}
	if out.body != lineFor("web1:22", alive)+"\n" {
		t.Errorf("body = %q; only the reachable host belongs in it", out.body)
	}
}

// THE GOVERNING REQUIREMENT ON #2592: "an unreachable host needs to answer from
// state and not fail".
//
// This is also the whole reason this is a managed resource rather than the data
// source the issue's title asks for: datasource.ReadRequest carries only
// Config, so there is no prior state for a data source to answer from.
func TestAnUnreachableHostAtRefreshKeepsItsPinnedEntry(t *testing.T) {
	pinnedKey := ed25519Signer(t).PublicKey()
	pinnedLine := lineFor("web2:22", pinnedKey)
	alive := lineFor("web1:22", ecdsaSigner(t).PublicKey())

	prior := map[string][]string{
		"web1": {alive},
		"web2": {pinnedLine},
	}
	fleet := fakeFleet{
		"web1:22": answered(mustParseLine(t, alive)),
		"web2:22": unreachable("dial tcp web2:22: connect: no route to host"),
	}

	out, diags := reconcile(context.Background(), req(target("web1"), target("web2")), prior, fleet.fetch)
	if diags.HasError() {
		t.Fatalf("errors: %v", diags.Errors())
	}

	if got := out.entries["web2"]; len(got) != 1 || got[0] != pinnedLine {
		t.Fatalf("entries[web2] = %v, want the pinned line kept from state", got)
	}
	if !strings.Contains(out.body, pinnedLine) {
		t.Error("the unreachable host's pinned line is missing from the body; the rest of the fleet " +
			"would be verified against a file that no longer vouches for it")
	}
	if got := out.unreachable; len(got) != 1 || got[0] != "web2" {
		t.Errorf("unreachable = %v, want [web2] -- it is still reported", got)
	}
	if out.moved {
		t.Error("keeping a pin is not pinning something new, so scanned_at must not advance")
	}
	// And the warning says what was kept, so the operator is not left guessing.
	if !strings.Contains(diags.Warnings()[0].Detail(), "kept from state") {
		t.Errorf("the warning should say the pin was kept: %q", diags.Warnings()[0].Detail())
	}
}

// ── A changed key ────────────────────────────────────────────────────────────

func TestAChangedKeyKeepsThePinAndIsReported(t *testing.T) {
	old := ed25519Signer(t).PublicKey()
	replacement := ed25519Signer(t).PublicKey()
	oldLine := lineFor("web1:22", old)

	prior := map[string][]string{"web1": {oldLine}}
	fleet := fakeFleet{"web1:22": answered(replacement)}

	out, diags := reconcile(context.Background(), req(target("web1")), prior, fleet.fetch)
	if diags.HasError() {
		t.Fatalf("a rotated key must not fail the read: %v", diags.Errors())
	}

	if got := out.entries["web1"]; len(got) != 1 || got[0] != oldLine {
		t.Fatalf("entries[web1] = %v, want the PINNED line kept", got)
	}
	if out.body != oldLine+"\n" {
		t.Errorf("body = %q; the offered key must not reach the file", out.body)
	}
	if got := out.changed; len(got) != 1 || got[0] != "web1" {
		t.Errorf("changed = %v, want [web1]", got)
	}
	if out.moved {
		t.Error("nothing was pinned, so scanned_at must not advance")
	}

	w := diags.Warnings()[0]
	if !strings.Contains(w.Detail(), oldLine) || !strings.Contains(w.Detail(), lineFor("web1:22", replacement)) {
		t.Errorf("the warning must show both the pinned and the offered key: %q", w.Detail())
	}
	// Re-pinning is a deliberate act, so the remedy has to be in the message.
	if !strings.Contains(w.Detail(), "-replace=") {
		t.Errorf("the warning must say how to re-pin: %q", w.Detail())
	}
}

// A host reported changed once, however many of its key types moved -- the set
// has to be unique or state is not byte-stable.
func TestAHostChangedOnSeveralKeyTypesIsReportedOnce(t *testing.T) {
	oldEd, oldEc := ed25519Signer(t).PublicKey(), ecdsaSigner(t).PublicKey()
	newEd, newEc := ed25519Signer(t).PublicKey(), ecdsaSigner(t).PublicKey()

	prior := map[string][]string{"web1": {lineFor("web1:22", oldEd), lineFor("web1:22", oldEc)}}
	fleet := fakeFleet{"web1:22": answered(newEd, newEc)}

	out, diags := reconcile(context.Background(), req(target("web1")), prior, fleet.fetch)
	if diags.HasError() {
		t.Fatalf("errors: %v", diags.Errors())
	}
	if got := out.changed; len(got) != 1 || got[0] != "web1" {
		t.Errorf("changed = %v, want [web1] exactly once", got)
	}
	// And the diagnostic has to agree with it about number. A detail line
	// is per changed key TYPE, so counting those said "2 hosts" for one
	// host -- a summary that contradicts the set beside it.
	if s := diags.Warnings()[0].Summary(); s != "1 host presented a different host key" {
		t.Errorf("summary = %q; it must count hosts, not key types", s)
	}
}

// A key type appearing for the FIRST time is not the threat a pin defends
// against -- the host grew an algorithm, it did not replace a key. So it is
// added rather than reported, which is what lets `key_types` be extended
// without replacing the resource.
func TestANewKeyTypeIsAddedRatherThanReportedChanged(t *testing.T) {
	ed := ed25519Signer(t).PublicKey()
	ec := ecdsaSigner(t).PublicKey()
	edLine := lineFor("web1:22", ed)

	prior := map[string][]string{"web1": {edLine}}
	fleet := fakeFleet{"web1:22": answered(ed, ec)}

	out := run(t, req(target("web1")), prior, fleet)

	if len(out.changed) != 0 {
		t.Errorf("changed = %v; a new key type is an addition, not a replacement", out.changed)
	}
	want := []string{edLine, lineFor("web1:22", ec)}
	if got := out.entries["web1"]; len(got) != 2 || got[0] != want[0] || got[1] != want[1] {
		t.Errorf("entries[web1] = %v, want the prior line then the new one", got)
	}
	if !out.moved {
		t.Error("adding a key type is pinning something new, so scanned_at must advance")
	}
}

// Narrowing `key_types` stops new types being added and un-pins NOTHING.
// Removing trust is deliberate.
func TestNarrowingKeyTypesUnpinsNothing(t *testing.T) {
	ed, ec := ed25519Signer(t).PublicKey(), ecdsaSigner(t).PublicKey()
	edLine, ecLine := lineFor("web1:22", ed), lineFor("web1:22", ec)

	prior := map[string][]string{"web1": {edLine, ecLine}}
	// Only ed25519 is asked for now, so only it answers.
	fleet := fakeFleet{"web1:22": answered(ed)}

	r := req(target("web1"))
	r.keyTypes = []string{ssh.KeyAlgoED25519}
	out, diags := reconcile(context.Background(), r, prior, fleet.fetch)
	if diags.HasError() {
		t.Fatalf("errors: %v", diags.Errors())
	}

	if got := out.entries["web1"]; len(got) != 2 {
		t.Errorf("entries[web1] = %v, want both pins kept", got)
	}
	if out.moved {
		t.Error("nothing new was pinned")
	}
}

// ── The host set ─────────────────────────────────────────────────────────────

// A host removed from `hosts` is dropped, which is what makes the resource
// declarative in its host set -- and the documented way to re-pin one host.
func TestAHostRemovedFromHostsIsDropped(t *testing.T) {
	a, b := ed25519Signer(t).PublicKey(), ecdsaSigner(t).PublicKey()
	prior := map[string][]string{
		"web1": {lineFor("web1:22", a)},
		"web2": {lineFor("web2:22", b)},
	}
	fleet := fakeFleet{"web1:22": answered(a), "web2:22": answered(b)}

	out := run(t, req(target("web1")), prior, fleet)

	if _, still := out.entries["web2"]; still {
		t.Error("web2 is no longer in `hosts` and must not be in the file")
	}
	if strings.Contains(out.body, "web2") {
		t.Errorf("body still mentions web2:\n%s", out.body)
	}
}

func TestAnEmptyHostSetIsAnEmptyFileAndNotAnError(t *testing.T) {
	out, diags := reconcile(context.Background(), req(), nil, fakeFleet{}.fetch)
	if diags.HasError() || len(diags.Warnings()) != 0 {
		t.Fatalf("an inventory that resolves to no hosts is legitimate: %v", diags)
	}
	if out.body != "" || len(out.entries) != 0 {
		t.Errorf("body = %q, entries = %v", out.body, out.entries)
	}
	// Empty rather than null, so a configuration need not also check for null.
	if out.unreachable == nil || out.changed == nil {
		t.Error("the reported sets must be empty rather than null")
	}
}

// ── Prior state this resource cannot read ────────────────────────────────────

// A hand-edited or corrupted line is KEPT -- state is not this resource's to
// discard -- and reported, because a line it cannot parse is one it cannot
// compare, so a scan could sit a second entry of the same type beside it.
func TestAnUnparseablePriorLineIsKeptAndReported(t *testing.T) {
	ed := ed25519Signer(t).PublicKey()
	prior := map[string][]string{"web1": {"this is not a known_hosts line at all"}}
	fleet := fakeFleet{"web1:22": answered(ed)}

	out, diags := reconcile(context.Background(), req(target("web1")), prior, fleet.fetch)
	if diags.HasError() {
		t.Fatalf("errors: %v", diags.Errors())
	}
	if got := out.entries["web1"]; len(got) != 2 || got[0] != prior["web1"][0] {
		t.Fatalf("entries[web1] = %v, want the unreadable line kept and the scan added", got)
	}
	var reported bool
	for _, w := range diags.Warnings() {
		if strings.Contains(w.Summary(), "cannot parse") {
			reported = true
		}
	}
	if !reported {
		t.Errorf("an unparseable line must be reported, got %v", diags.Warnings())
	}
}

// ── parseHosts ───────────────────────────────────────────────────────────────

func TestParseHostsAcceptsTheFormsItDocuments(t *testing.T) {
	for _, c := range []struct {
		name, entry, wantAddress string
	}{
		{"bare name", "web1.example.com", "web1.example.com:22"},
		{"name and port", "web1.example.com:2222", "web1.example.com:2222"},
		{"ipv4", "10.0.0.1", "10.0.0.1:22"},
		{"ipv4 and port", "10.0.0.1:2222", "10.0.0.1:2222"},
		{"bare ipv6", "2001:db8::1", "[2001:db8::1]:22"},
		{"bracketed ipv6 and port", "[2001:db8::1]:2222", "[2001:db8::1]:2222"},
		{"surrounding whitespace is trimmed", "  web1.example.com  ", "web1.example.com:22"},
	} {
		t.Run(c.name, func(t *testing.T) {
			hosts, diags := parseHosts([]string{c.entry}, defaultPort)
			if diags.HasError() {
				t.Fatalf("%q was refused: %v", c.entry, diags.Errors())
			}
			if len(hosts) != 1 {
				t.Fatalf("got %d hosts", len(hosts))
			}
			if hosts[0].address != c.wantAddress {
				t.Errorf("address = %q, want %q", hosts[0].address, c.wantAddress)
			}
			if hosts[0].key != strings.TrimSpace(c.entry) {
				t.Errorf("key = %q, want the entry as written", hosts[0].key)
			}
		})
	}
}

func TestParseHostsRefusesWhatItCannotAddress(t *testing.T) {
	for _, c := range []struct{ name, entry, wantIn string }{
		{"empty", "", "empty"},
		{"whitespace", "   ", "empty"},
		{"no port after the colon", "web1:", "not a number"},
		{"a service name", "web1:ssh", "not a number"},
		{"port zero", "web1:0", "outside 1-65535"},
		{"port too high", "web1:70000", "outside 1-65535"},
		{"a comma, which separates hosts in a known_hosts line", "web1,web2", "separator"},
		{"a hash, which starts a comment", "web1#x", "separator"},
		{"an inner space", "web 1", "separator"},
	} {
		t.Run(c.name, func(t *testing.T) {
			_, diags := parseHosts([]string{c.entry}, defaultPort)
			if !diags.HasError() {
				t.Fatalf("%q was accepted", c.entry)
			}
			joined := diags.Errors()[0].Summary() + " " + diags.Errors()[0].Detail()
			if !strings.Contains(joined, c.wantIn) {
				t.Errorf("the error does not say why: %q", joined)
			}
		})
	}
}

// "web1" and "web1:22" are two members of a Terraform set and one host. Letting
// both through would have one pin over the other, with set ordering deciding
// which -- so it is refused, naming both entries.
func TestParseHostsRefusesTwoEntriesForOneHost(t *testing.T) {
	_, diags := parseHosts([]string{"web1", "web1:22"}, defaultPort)
	if !diags.HasError() {
		t.Fatal("two spellings of one host were accepted")
	}
	s := diags.Errors()[0].Summary()
	if !strings.Contains(s, "web1") || !strings.Contains(s, "web1:22") {
		t.Errorf("the error must name both entries: %q", s)
	}
}

func TestParseHostsAppliesThePortDefaultAndLetsAnEntryOverrideIt(t *testing.T) {
	hosts, diags := parseHosts([]string{"web1", "web2:2022"}, 2222)
	if diags.HasError() {
		t.Fatalf("errors: %v", diags.Errors())
	}
	if hosts[0].address != "web1:2222" {
		t.Errorf("web1 = %q, want the resource's `port`", hosts[0].address)
	}
	if hosts[1].address != "web2:2022" {
		t.Errorf("web2 = %q, want its own port", hosts[1].address)
	}
}

// ── parseKeyTypes ────────────────────────────────────────────────────────────

func TestParseKeyTypesRefusesAnUnknownAlgorithmByName(t *testing.T) {
	_, diags := parseKeyTypes([]string{ssh.KeyAlgoED25519, "ed25519"})
	if !diags.HasError() {
		t.Fatal("`ed25519` was accepted; it is ssh-keyscan's -t alias, not an algorithm name")
	}
	e := diags.Errors()[0]
	if !strings.Contains(e.Summary(), "ed25519") {
		t.Errorf("the error must name the value: %q", e.Summary())
	}
	// And it must say what IS accepted, or the operator is left guessing.
	for _, want := range supportedKeyTypes {
		if !strings.Contains(e.Detail(), want) {
			t.Errorf("the error does not list %q as accepted: %q", want, e.Detail())
		}
	}
}

func TestParseKeyTypesDefaultsAndDeduplicates(t *testing.T) {
	got, diags := parseKeyTypes(nil)
	if diags.HasError() {
		t.Fatalf("errors: %v", diags.Errors())
	}
	if strings.Join(got, ",") != strings.Join(defaultKeyTypes, ",") {
		t.Errorf("default = %v, want %v", got, defaultKeyTypes)
	}

	got, diags = parseKeyTypes([]string{ssh.KeyAlgoED25519, ssh.KeyAlgoED25519})
	if diags.HasError() {
		t.Fatalf("errors: %v", diags.Errors())
	}
	if len(got) != 1 {
		t.Errorf("a repeated algorithm must be asked for once, got %v", got)
	}
}

// The schema's declared default has to be inside the set the validator accepts,
// or the one configuration that names nothing is the one that is refused.
func TestTheDefaultKeyTypesAreAllAccepted(t *testing.T) {
	if _, diags := parseKeyTypes(defaultKeyTypes); diags.HasError() {
		t.Fatalf("the default is not accepted by the validator: %v", diags.Errors())
	}
	var declared []string
	for _, v := range defaultKeyTypesValue().Elements() {
		declared = append(declared, strings.Trim(v.String(), `"`))
	}
	if strings.Join(declared, ",") != strings.Join(defaultKeyTypes, ",") {
		t.Errorf("the schema default %v does not match defaultKeyTypes %v", declared, defaultKeyTypes)
	}
}

// This is the case that distinguishes the ENTRY from the resolved ADDRESS, and
// it took a mutation to find: with port 22 the two render identically, because
// knownhosts.Normalize collapses "web1" and "web1:22" to the same host field.
// A port supplied by the resource rather than written into the entry is where
// they come apart -- and the line's host field is what ssh matches on, so
// `web1 ssh-ed25519 …` for a host on 2222 would be a line that never matches.
func TestTheResourcePortReachesTheLineThoughTheEntryDoesNotCarryIt(t *testing.T) {
	key := ed25519Signer(t).PublicKey()

	hosts, diags := parseHosts([]string{"web1"}, 2222)
	if diags.HasError() {
		t.Fatalf("errors: %v", diags.Errors())
	}
	r := aRequest{hosts: hosts, keyTypes: defaultKeyTypes, timeout: time.Second}
	out := run(t, r, nil, fakeFleet{"web1:2222": answered(key)})

	if !strings.HasPrefix(out.body, "[web1]:2222 ") {
		t.Errorf("body = %q, want it to start with the bracket form \"[web1]:2222 \"", out.body)
	}
	// And the map key stays the entry as WRITTEN, which is the other half of
	// the same distinction: an operator indexes `entries` with "web1".
	if _, ok := out.entries["web1"]; !ok {
		t.Errorf("entries is keyed %v, want the entry as written", keysOfSlice(out.entries))
	}
}

func keysOfSlice(m map[string][]string) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

// ── Helpers ──────────────────────────────────────────────────────────────────

func mustParseLine(t *testing.T, line string) ssh.PublicKey {
	t.Helper()
	_, _, pub, _, _, err := ssh.ParseKnownHosts([]byte(line + "\n"))
	if err != nil {
		t.Fatalf("parsing %q: %v", line, err)
	}
	return pub
}

func sortedAscending(lines []string) bool {
	for i := 1; i < len(lines); i++ {
		if lines[i-1] > lines[i] {
			return false
		}
	}
	return true
}
