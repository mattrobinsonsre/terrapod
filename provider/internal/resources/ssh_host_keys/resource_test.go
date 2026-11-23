package ssh_host_keys

import (
	"context"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	rschema "github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/tfsdk"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-go/tftypes"
)

// ── Schema ───────────────────────────────────────────────────────────────────

func TestSchemaIsExactlyTheseAttributes(t *testing.T) {
	attrs := schemaOf(t).Attributes

	want := map[string]struct{ required, optional, computed bool }{
		"id":                   {false, false, true},
		"hosts":                {true, false, false},
		"port":                 {false, true, true},
		"key_types":            {false, true, true},
		"scan_timeout_seconds": {false, true, true},
		"pinned_entries":       {false, true, false},
		"known_hosts":          {false, false, true},
		"entries":              {false, false, true},
		"unreachable_hosts":    {false, false, true},
		"changed_hosts":        {false, false, true},
		"scanned_at":           {false, false, true},
	}
	if len(attrs) != len(want) {
		t.Errorf("attribute count = %d, want %d: %v", len(attrs), len(want), keysOf(attrs))
	}
	for name, w := range want {
		a, ok := attrs[name]
		if !ok {
			t.Errorf("attribute %q is missing", name)
			continue
		}
		if a.IsRequired() != w.required || a.IsOptional() != w.optional || a.IsComputed() != w.computed {
			t.Errorf("%s: required=%t optional=%t computed=%t, want %t/%t/%t",
				name, a.IsRequired(), a.IsOptional(), a.IsComputed(), w.required, w.optional, w.computed)
		}
	}
}

// NOTHING here is sensitive, and that is a decision rather than an omission. A
// host's PUBLIC key is public -- it is what the host hands to anyone who
// connects -- and the `known_hosts` field on the machine credential is itself
// deliberately not redacted in a run's log for the same reason. Marking this
// sensitive would hide the one value an operator most needs to read when a host
// is refused, and would redact it out of every plan.
func TestNothingIsSensitive(t *testing.T) {
	for name, a := range schemaOf(t).Attributes {
		if a.IsSensitive() {
			t.Errorf("%s is marked sensitive; a host's public key is public", name)
		}
	}
}

// `pinned_entries` is the one attribute that is Optional and NOT Computed, so
// it is the only one the empty-string round-trip class could apply to -- and it
// is a map rather than a string, so it cannot. Pinned here because the reasoning
// is easy to lose: making it Computed to "be consistent" would mean a removed
// pin reads back as the value the server last saw, and there is no server.
func TestPinnedEntriesIsNotComputed(t *testing.T) {
	a := schemaOf(t).Attributes["pinned_entries"]
	if a.IsComputed() {
		t.Error("pinned_entries must not be Computed: removing a pin has to remove it, and there is " +
			"no server whose value could be read back instead")
	}
}

// The resource talks to NO SERVER. That is what makes it usable when the
// Terrapod API is unreachable and without any RBAC permission beyond what the
// provider block itself needs -- so a plan can still pin host keys during an
// outage. A client import here would quietly cost that.
func TestThePackageTalksToNoServer(t *testing.T) {
	fset := token.NewFileSet()
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatalf("reading the package: %v", err)
	}
	checked := 0
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		f, parseErr := parser.ParseFile(fset, filepath.Join(".", name), nil, parser.ImportsOnly)
		if parseErr != nil {
			t.Fatalf("parsing %s: %v", name, parseErr)
		}
		checked++
		for _, imp := range f.Imports {
			path := strings.Trim(imp.Path.Value, `"`)
			if strings.Contains(path, "go-terrapod") || strings.HasSuffix(path, "internal/client") {
				t.Errorf("%s imports %q. This resource owns nothing on the server and must keep "+
					"working when the API is unreachable; reaching the API would make a host-key pin "+
					"depend on an outage it has nothing to do with", name, path)
			}
		}
	}
	// A floor, so a walk that stops finding the package fails here rather than
	// passing on nothing.
	if checked < 3 {
		t.Fatalf("walked only %d non-test files; the walk is not finding the package", checked)
	}
}

// ── Create ───────────────────────────────────────────────────────────────────

func TestCreatePinsEveryReachableHostAndSetsState(t *testing.T) {
	ctx := context.Background()
	a := ed25519Signer(t).PublicKey()
	r := &sshHostKeysResource{fetch: fakeFleet{"web1:22": answered(a)}.fetch}

	resp := resource.CreateResponse{State: nullState(t)}
	r.Create(ctx, resource.CreateRequest{Plan: planWith(t, hostSet("web1"))}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("create: %v", resp.Diagnostics.Errors())
	}

	got := stateModel(t, resp.State)
	if got.KnownHosts.ValueString() != lineFor("web1:22", a)+"\n" {
		t.Errorf("known_hosts = %q", got.KnownHosts.ValueString())
	}
	if !strings.HasPrefix(got.ID.ValueString(), "sshkeys-") {
		t.Errorf("id = %q, want the sshkeys- prefix", got.ID.ValueString())
	}
	// RFC3339 with a trailing Z, which is the house format for every timestamp
	// the provider writes.
	at := got.ScannedAt.ValueString()
	if _, err := time.Parse(time.RFC3339, at); err != nil || !strings.HasSuffix(at, "Z") {
		t.Errorf("scanned_at = %q, want RFC3339 UTC with a trailing Z (parse err %v)", at, err)
	}
	// Every collection is set, never left unknown: an unknown value in state
	// after apply is "Provider produced inconsistent result".
	for name, v := range map[string]attr.Value{
		"entries": got.Entries, "unreachable_hosts": got.UnreachableHosts,
		"changed_hosts": got.ChangedHosts,
	} {
		if v.IsUnknown() {
			t.Errorf("%s was left unknown", name)
		}
	}
}

// A create against a fleet that is entirely unreachable still creates: the
// known_hosts body is empty, every host is listed, and it is a warning.
func TestCreateAgainstAnUnreachableFleetStillSucceeds(t *testing.T) {
	ctx := context.Background()
	r := &sshHostKeysResource{fetch: fakeFleet{}.fetch}

	resp := resource.CreateResponse{State: nullState(t)}
	r.Create(ctx, resource.CreateRequest{Plan: planWith(t, hostSet("web1", "web2"))}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("an unreachable fleet must not fail the create: %v", resp.Diagnostics.Errors())
	}
	if len(resp.Diagnostics.Warnings()) == 0 {
		t.Fatal("it must warn: a silently empty known_hosts file is how a configure comes to fail " +
			"for a reason nobody connects to this resource")
	}
	if got := resp.Diagnostics.Warnings()[0].Summary(); got != "2 hosts could not be reached" {
		t.Errorf("summary = %q; it must count hosts and agree with itself about number", got)
	}
	got := stateModel(t, resp.State)
	if got.KnownHosts.ValueString() != "" {
		t.Errorf("known_hosts = %q, want empty", got.KnownHosts.ValueString())
	}
	if len(got.UnreachableHosts.Elements()) != 2 {
		t.Errorf("unreachable_hosts = %v, want both hosts", got.UnreachableHosts)
	}
}

func TestCreateRefusesAnUnknownKeyType(t *testing.T) {
	ctx := context.Background()
	r := &sshHostKeysResource{fetch: fakeFleet{}.fetch}

	m := hostSet("web1")
	m.KeyTypes = types.ListValueMust(types.StringType, []attr.Value{types.StringValue("ed25519")})

	resp := resource.CreateResponse{State: nullState(t)}
	r.Create(ctx, resource.CreateRequest{Plan: planWith(t, m)}, &resp)
	if !resp.Diagnostics.HasError() {
		t.Fatal("an unknown algorithm must be an error, not a warning: it cannot be interpreted at all")
	}
}

func TestCreateRefusesANonPositiveTimeout(t *testing.T) {
	ctx := context.Background()
	r := &sshHostKeysResource{fetch: fakeFleet{}.fetch}

	m := hostSet("web1")
	m.ScanTimeoutSeconds = types.Int64Value(0)

	resp := resource.CreateResponse{State: nullState(t)}
	r.Create(ctx, resource.CreateRequest{Plan: planWith(t, m)}, &resp)
	if !resp.Diagnostics.HasError() {
		t.Fatal("a zero timeout would give every dial a deadline in the past, so every host would " +
			"read as unreachable; it must be refused rather than silently doing that")
	}
}

// ── Read ─────────────────────────────────────────────────────────────────────

// The wiring half of the governing requirement, end to end through the
// framework rather than through reconcile alone: Read must recover the prior
// pins out of state and hand them to the decision, or an unreachable host
// loses its entry however correct reconcile is.
func TestReadKeepsAPinnedEntryForAHostThatHasGoneAway(t *testing.T) {
	ctx := context.Background()
	pinned := lineFor("web1:22", ed25519Signer(t).PublicKey())

	prior := hostSet("web1")
	prior.ID = types.StringValue("sshkeys-deadbeef")
	prior.KnownHosts = types.StringValue(pinned + "\n")
	prior.Entries = types.MapValueMust(types.StringType, map[string]attr.Value{
		"web1": types.StringValue(pinned),
	})
	prior.ScannedAt = types.StringValue("2026-01-01T00:00:00Z")

	// Nothing answers.
	r := &sshHostKeysResource{fetch: fakeFleet{}.fetch}
	resp := resource.ReadResponse{State: nullState(t)}
	r.Read(ctx, resource.ReadRequest{State: stateWith(t, prior)}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("read: %v", resp.Diagnostics.Errors())
	}

	got := stateModel(t, resp.State)
	if got.KnownHosts.ValueString() != pinned+"\n" {
		t.Errorf("known_hosts = %q, want the pin kept from state", got.KnownHosts.ValueString())
	}
	if got.ID.ValueString() != "sshkeys-deadbeef" {
		t.Errorf("id = %q; a refresh must not mint a new one", got.ID.ValueString())
	}
	if got.ScannedAt.ValueString() != "2026-01-01T00:00:00Z" {
		t.Errorf("scanned_at = %q; nothing was pinned, so it must not advance",
			got.ScannedAt.ValueString())
	}
	if len(got.UnreachableHosts.Elements()) != 1 {
		t.Errorf("unreachable_hosts = %v, want [web1]", got.UnreachableHosts)
	}
}

// The round trip through `entries` is what the whole refresh rests on: a host's
// lines are stored joined by newlines and split back out, and a line cannot
// contain a newline, so it is exact.
func TestTheEntriesRoundTripRecoversEveryLine(t *testing.T) {
	ctx := context.Background()
	one := lineFor("web1:22", ed25519Signer(t).PublicKey())
	two := lineFor("web1:22", ecdsaSigner(t).PublicKey())

	joined := joinedEntries(map[string][]string{"web1": {one, two}})
	m := types.MapValueMust(types.StringType, map[string]attr.Value{
		"web1": types.StringValue(joined["web1"]),
	})
	back, diags := priorEntries(ctx, m)
	if diags.HasError() {
		t.Fatalf("%v", diags.Errors())
	}
	if len(back["web1"]) != 2 || back["web1"][0] != one || back["web1"][1] != two {
		t.Errorf("round trip = %v, want [%q %q]", back["web1"], one, two)
	}

	// A null map is a create, not an error.
	back, diags = priorEntries(ctx, types.MapNull(types.StringType))
	if diags.HasError() || len(back) != 0 {
		t.Errorf("a null entries map must read as nothing pinned, got %v / %v", back, diags)
	}
}

// ── Update ───────────────────────────────────────────────────────────────────

// An update takes the host set from the PLAN and the pins from STATE, which is
// what makes it additive: added hosts are scanned, retained hosts keep their
// pin even while unreachable, and removed hosts are dropped.
func TestUpdateScansAdditionsKeepsRetainedPinsAndDropsRemovals(t *testing.T) {
	ctx := context.Background()
	keep := lineFor("web1:22", ed25519Signer(t).PublicKey())
	gone := lineFor("web2:22", ecdsaSigner(t).PublicKey())
	fresh := ed25519Signer(t).PublicKey()

	state := hostSet("web1", "web2")
	state.ID = types.StringValue("sshkeys-aaaa")
	state.ScannedAt = types.StringValue("2026-01-01T00:00:00Z")
	state.Entries = types.MapValueMust(types.StringType, map[string]attr.Value{
		"web1": types.StringValue(keep),
		"web2": types.StringValue(gone),
	})

	// web1 stays and is now unreachable; web2 is removed; web3 is added.
	plan := hostSet("web1", "web3")
	r := &sshHostKeysResource{fetch: fakeFleet{"web3:22": answered(fresh)}.fetch}

	resp := resource.UpdateResponse{State: nullState(t)}
	r.Update(ctx, resource.UpdateRequest{Plan: planWith(t, plan), State: stateWith(t, state)}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("update: %v", resp.Diagnostics.Errors())
	}

	got := stateModel(t, resp.State)
	body := got.KnownHosts.ValueString()
	if !strings.Contains(body, keep) {
		t.Error("a retained host must keep its pin even while unreachable")
	}
	if strings.Contains(body, gone) {
		t.Error("a host removed from `hosts` must be dropped")
	}
	if !strings.Contains(body, lineFor("web3:22", fresh)) {
		t.Error("an added host must be scanned and pinned")
	}
	if got.ID.ValueString() != "sshkeys-aaaa" {
		t.Errorf("id = %q; an update must carry it, not mint a new one", got.ID.ValueString())
	}
	// web3 was pinned, so this read DID move the pinned set.
	if got.ScannedAt.ValueString() == "2026-01-01T00:00:00Z" {
		t.Error("pinning a new host must advance scanned_at")
	}
}

// Nothing is torn down. A pin is a record of what was observed, and the hosts
// are not this resource's to change.
func TestDeleteTouchesNothing(t *testing.T) {
	r := &sshHostKeysResource{fetch: func(context.Context, string, []string, time.Duration) scanResult {
		t.Error("delete must open no connections")
		return scanResult{}
	}}
	var resp resource.DeleteResponse
	r.Delete(context.Background(), resource.DeleteRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Errorf("delete: %v", resp.Diagnostics.Errors())
	}
}

// It owns nothing on the server, so there is nothing to import -- an id would
// recover no pins, and a resource whose import produced an empty known_hosts
// file would be worse than one that refuses.
func TestThereIsNoImport(t *testing.T) {
	if _, ok := NewResource().(resource.ResourceWithImportState); ok {
		t.Error("this resource supports import; there is nothing on the server to import FROM, and " +
			"an import with no pins would produce an empty known_hosts file")
	}
}

func TestTheProductionResourceDialsForReal(t *testing.T) {
	r, ok := NewResource().(*sshHostKeysResource)
	if !ok {
		t.Fatalf("NewResource returned %T", NewResource())
	}
	if r.fetch == nil {
		t.Fatal("the production resource has no fetcher wired, so every host would read as unreachable")
	}
}

// ── Scaffolding ──────────────────────────────────────────────────────────────

func schemaOf(t *testing.T) rschema.Schema {
	t.Helper()
	var resp resource.SchemaResponse
	NewResource().Schema(context.Background(), resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema: %v", resp.Diagnostics)
	}
	return resp.Schema
}

func nullObject(t *testing.T) tftypes.Value {
	t.Helper()
	objType, ok := schemaOf(t).Type().TerraformType(context.Background()).(tftypes.Object)
	if !ok {
		t.Fatal("the schema's terraform type is not an object")
	}
	attrs := make(map[string]tftypes.Value, len(objType.AttributeTypes))
	for name, ty := range objType.AttributeTypes {
		attrs[name] = tftypes.NewValue(ty, nil)
	}
	return tftypes.NewValue(objType, attrs)
}

func nullState(t *testing.T) tfsdk.State {
	t.Helper()
	return tfsdk.State{Schema: schemaOf(t), Raw: nullObject(t)}
}

func planWith(t *testing.T, m sshHostKeysModel) tfsdk.Plan {
	t.Helper()
	p := tfsdk.Plan{Schema: schemaOf(t), Raw: nullObject(t)}
	if d := p.Set(context.Background(), &m); d.HasError() {
		t.Fatalf("building the plan: %v", d)
	}
	return p
}

func stateWith(t *testing.T, m sshHostKeysModel) tfsdk.State {
	t.Helper()
	s := nullState(t)
	if d := s.Set(context.Background(), &m); d.HasError() {
		t.Fatalf("building the state: %v", d)
	}
	return s
}

func stateModel(t *testing.T, s tfsdk.State) sshHostKeysModel {
	t.Helper()
	var m sshHostKeysModel
	if d := s.Get(context.Background(), &m); d.HasError() {
		t.Fatalf("reading the state back: %v", d)
	}
	return m
}

// hostSet is a model carrying only what a configuration supplies, with the
// defaults filled in the way the schema would.
func hostSet(hosts ...string) sshHostKeysModel {
	elems := make([]attr.Value, 0, len(hosts))
	for _, h := range hosts {
		elems = append(elems, types.StringValue(h))
	}
	return sshHostKeysModel{
		ID:                 types.StringNull(),
		Hosts:              types.SetValueMust(types.StringType, elems),
		Port:               types.Int64Value(defaultPort),
		KeyTypes:           defaultKeyTypesValue(),
		ScanTimeoutSeconds: types.Int64Value(defaultScanTimeoutSeconds),
		PinnedEntries:      types.MapNull(types.StringType),
		KnownHosts:         types.StringNull(),
		Entries:            types.MapNull(types.StringType),
		UnreachableHosts:   types.SetNull(types.StringType),
		ChangedHosts:       types.SetNull(types.StringType),
		ScannedAt:          types.StringNull(),
	}
}

func keysOf[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
