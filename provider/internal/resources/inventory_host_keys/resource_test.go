package inventory_host_keys

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	rschema "github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/tfsdk"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-go/tftypes"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// Real blobs: the server checks a blob's own embedded algorithm name against
// the declared type, so a placeholder would pass through this provider and be
// refused by the API.
const (
	edKey    = "AAAAC3NzaC1lZDI1NTE5AAAAIFNluHur5rQdQnsTP1TO9Ufn1yB6H/Ax9J25aVber+U6"
	edKeyNew = "AAAAC3NzaC1lZDI1NTE5AAAAIGnOPgMYxyAPzBg9kbPsBhFoTsDRTY1eDiRYNDpQsVN3"
	ecKey    = "AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBPCxa530CiYpvxDx" +
		"cvhEGWb0kWZjjkGu9M4nexti6vKt9JpjDKk8gNIRRTf6Lg4hj7FhqBBbKR8GXr0P5CHU/Hg="
)

// ── Schema ───────────────────────────────────────────────────────────────────

func TestSchemaShape(t *testing.T) {
	s := schemaOf(t)

	if len(s.Attributes) != 2 {
		t.Errorf("want exactly id and host_id as attributes, got %v", s.Attributes)
	}
	for _, name := range []string{"id", "host_id"} {
		if _, ok := s.Attributes[name]; !ok {
			t.Errorf("attribute %q is missing", name)
		}
	}
	if !s.Attributes["host_id"].IsRequired() {
		t.Error("host_id is required: a host id is not something the provider default can supply")
	}
	if !s.Attributes["id"].IsComputed() || s.Attributes["id"].IsOptional() {
		t.Error("id must be computed-only")
	}

	// The keys are a repeated BLOCK, not a list attribute, which is what lets a
	// configuration write `key { … }` per key.
	block, ok := s.Blocks["key"].(rschema.SetNestedBlock)
	if !ok {
		t.Fatalf("key must be a SetNestedBlock, got %T", s.Blocks["key"])
	}
	// A SET, not a list: a trust set has no order, and a list would plan a diff
	// whenever two blocks were swapped in the configuration.
	if !strings.HasPrefix(block.Type().String(), "types.SetType[") {
		t.Errorf("key must be set-typed, got %s", block.Type().String())
	}

	attrs := block.NestedObject.Attributes
	if len(attrs) != 3 {
		t.Errorf("want exactly key_type, public_key and marker, got %v", attrs)
	}
	// The names the API and the terrapod_ssh_host_keys data source both use, so
	// the two compose.
	for _, name := range []string{"key_type", "public_key", "marker"} {
		if _, ok := attrs[name]; !ok {
			t.Errorf("block attribute %q is missing", name)
		}
	}
	if !attrs["key_type"].IsRequired() || !attrs["public_key"].IsRequired() {
		t.Error("key_type and public_key are the key's identity and are required")
	}
	// A host's public key is public. Masking it would hide the one value an
	// operator needs when a host presents something else.
	if attrs["public_key"].IsSensitive() {
		t.Error("public_key must not be sensitive")
	}
}

// `marker` is Optional and NOT Computed, and no attribute inside the block is
// Computed at all.
//
// Two different reasons, both about set semantics. A set element's identity is
// its whole value, so a Computed attribute inside one leaves the planned
// element unknown and Terraform can no longer match a configuration element to
// a state element. And the server has no value to compute for `marker`: it
// stores null for an ordinary entry, so Optional+Computed would plan an absent
// marker as unknown and settle it as null, a diff that never converges.
func TestNoBlockAttributeIsComputed(t *testing.T) {
	block := keyBlock(t)
	for name, a := range block.NestedObject.Attributes {
		if a.IsComputed() {
			t.Errorf("%q must not be computed: a computed attribute inside a set element "+
				"leaves the planned element unknown and the set unmatchable", name)
		}
	}
	if !block.NestedObject.Attributes["marker"].IsOptional() {
		t.Error("marker must be optional: an ordinary entry has none")
	}
}

func TestOnlyTheHostForcesReplacement(t *testing.T) {
	ctx := context.Background()
	s := schemaOf(t)
	a, ok := s.Attributes["host_id"].(rschema.StringAttribute)
	if !ok {
		t.Fatalf("host_id is not a StringAttribute")
	}
	var replaces bool
	for _, pm := range a.PlanModifiers {
		if strings.Contains(pm.Description(ctx), "destroy and recreate") {
			replaces = true
		}
	}
	if !replaces {
		t.Error("host_id must force a replacement: a trust set belongs to one host")
	}
}

// Import is by HOST id, and must set `host_id` as well as `id` -- the read
// lists from the host, so an import that set only `id` would read nothing and
// silently report an empty trust set.
func TestImportSetsBothTheIDAndTheHost(t *testing.T) {
	ctx := context.Background()
	r, ok := NewResource().(resource.ResourceWithImportState)
	if !ok {
		t.Fatal("the resource must support import")
	}
	resp := resource.ImportStateResponse{State: nullState(t)}
	r.ImportState(ctx, resource.ImportStateRequest{ID: "invhost-abc"}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("import: %v", resp.Diagnostics)
	}
	for _, name := range []string{"id", "host_id"} {
		var got types.String
		if d := resp.State.GetAttribute(ctx, path.Root(name), &got); d.HasError() {
			t.Fatalf("reading %s: %v", name, d)
		}
		if got.ValueString() != "invhost-abc" {
			t.Errorf("%s = %q, want the host id", name, got.ValueString())
		}
	}
}

// ── The marker validator ─────────────────────────────────────────────────────

func TestMarkerProblem(t *testing.T) {
	for _, c := range []struct{ value, wantSubstring string }{
		{"cert-authority", ""},
		{"revoked", ""},
		// Refused rather than accepted-and-normalised: the server stores an
		// empty marker as no marker, so "" would differ from the configuration
		// on every subsequent plan.
		{"", "Omit the attribute entirely"},
		{"retired", "is not one OpenSSH defines"},
		// Terrapod renders the '@' itself, so a marker carrying one is wrong.
		{"@revoked", "is not one OpenSSH defines"},
		{"REVOKED", "is not one OpenSSH defines"},
	} {
		got := markerProblem(c.value)
		if c.wantSubstring == "" {
			if got != "" {
				t.Errorf("markerProblem(%q) = %q, want accepted", c.value, got)
			}
			continue
		}
		if !strings.Contains(got, c.wantSubstring) {
			t.Errorf("markerProblem(%q) = %q, want it to mention %q", c.value, got, c.wantSubstring)
		}
	}
}

// ── markerOf / markerValue: the two directions of one normalisation ──────────

func TestMarkerRoundTripsThroughNull(t *testing.T) {
	// Terraform null (an omitted attribute) is what the server stores as no
	// marker.
	if markerOf(types.StringNull()) != "" || markerOf(types.StringUnknown()) != "" {
		t.Error("an absent marker must be sent as the empty string")
	}
	if markerOf(types.StringValue("revoked")) != "revoked" {
		t.Error("a marker must be sent as itself")
	}
	// And back: the server's empty marker is an ABSENT attribute, not an empty
	// string. Projecting "" would make every unmarked key differ from the
	// configuration that omitted the attribute -- a permanent diff on almost
	// every key.
	if !markerValue("").IsNull() {
		t.Errorf("an empty stored marker must read back as null, got %v", markerValue(""))
	}
	if markerValue("revoked").ValueString() != "revoked" {
		t.Error("a stored marker must read back as itself")
	}
}

// ── The set diff ─────────────────────────────────────────────────────────────

func TestPlanChangesDeclaresWhatIsMissing(t *testing.T) {
	changes := planChanges(
		[]hostKeyBlockModel{block("ssh-ed25519", edKey, ""), block("ecdsa-sha2-nistp256", ecKey, "")},
		nil,
	)
	if len(changes.create) != 2 || len(changes.remark) != 0 || len(changes.delete) != 0 {
		t.Fatalf("want two creates and nothing else, got %+v", changes)
	}
}

func TestPlanChangesDoesNothingWhenTheSetMatches(t *testing.T) {
	changes := planChanges(
		[]hostKeyBlockModel{block("ssh-ed25519", edKey, ""), block("ssh-ed25519", edKeyNew, "revoked")},
		[]terrapod.InventoryHostKey{
			row("invhkey-1", "ssh-ed25519", edKey, ""),
			row("invhkey-2", "ssh-ed25519", edKeyNew, "revoked"),
		},
	)
	if len(changes.create) != 0 || len(changes.remark) != 0 || len(changes.delete) != 0 {
		t.Fatalf("a matching set needs no calls, got %+v", changes)
	}
}

// An unmarked block against an unmarked row must compare EQUAL. The block's
// marker is null and the row's is "", so a comparison that did not collapse
// them would re-mark every ordinary key on every apply.
func TestPlanChangesTreatsANullMarkerAsNoMarker(t *testing.T) {
	changes := planChanges(
		[]hostKeyBlockModel{{
			KeyType:   types.StringValue("ssh-ed25519"),
			PublicKey: types.StringValue(edKey),
			Marker:    types.StringNull(),
		}},
		[]terrapod.InventoryHostKey{row("invhkey-1", "ssh-ed25519", edKey, "")},
	)
	if len(changes.remark) != 0 {
		t.Errorf("a null marker and an unmarked row are the same thing: %+v", changes.remark)
	}
}

// A marker-only edit is a PATCH, not a destroy and declare. The replace would
// withdraw the key and re-add it, and between the two the host presents
// something nothing approves.
func TestPlanChangesRemarksInPlace(t *testing.T) {
	changes := planChanges(
		[]hostKeyBlockModel{block("ssh-ed25519", edKey, "revoked")},
		[]terrapod.InventoryHostKey{row("invhkey-1", "ssh-ed25519", edKey, "")},
	)
	if len(changes.create) != 0 || len(changes.delete) != 0 {
		t.Fatalf("a marker change must not replace the key: %+v", changes)
	}
	if len(changes.remark) != 1 ||
		changes.remark[0].id != "invhkey-1" || changes.remark[0].marker != "revoked" {
		t.Errorf("remark = %+v", changes.remark)
	}
}

// Clearing a marker is a re-mark to "" rather than no call at all -- the SDK
// sends it, and the server reads a supplied empty marker as "remove it".
func TestPlanChangesClearsAMarker(t *testing.T) {
	changes := planChanges(
		[]hostKeyBlockModel{block("ssh-ed25519", edKey, "")},
		[]terrapod.InventoryHostKey{row("invhkey-1", "ssh-ed25519", edKey, "revoked")},
	)
	if len(changes.remark) != 1 || changes.remark[0].marker != "" {
		t.Errorf("clearing a marker must be an in-place change to \"\": %+v", changes.remark)
	}
}

// A key on the host that the configuration does not declare is DRIFT, removed.
// That is the behaviour a trust anchor wants: the alternative is a key nobody
// approved quietly staying trusted.
func TestPlanChangesWithdrawsAnUndeclaredKey(t *testing.T) {
	changes := planChanges(
		[]hostKeyBlockModel{block("ssh-ed25519", edKey, "")},
		[]terrapod.InventoryHostKey{
			row("invhkey-1", "ssh-ed25519", edKey, ""),
			row("invhkey-2", "ssh-rsa", "AAAAB3NzaC1yc2EAAAA", ""),
		},
	)
	if len(changes.create) != 0 || len(changes.remark) != 0 {
		t.Fatalf("nothing to declare or re-mark: %+v", changes)
	}
	if len(changes.delete) != 1 || changes.delete[0] != "invhkey-2" {
		t.Errorf("delete = %v, want the undeclared key", changes.delete)
	}
}

// Declaring NO keys is a legitimate statement, and it withdraws everything.
func TestPlanChangesWithdrawsEverythingWhenNothingIsDeclared(t *testing.T) {
	changes := planChanges(nil, []terrapod.InventoryHostKey{
		row("invhkey-1", "ssh-ed25519", edKey, ""),
		row("invhkey-2", "ecdsa-sha2-nistp256", ecKey, ""),
	})
	if len(changes.delete) != 2 {
		t.Fatalf("want both withdrawn, got %+v", changes)
	}
	// Sorted, so an apply's calls are reproducible -- a map's iteration order
	// is not.
	if changes.delete[0] != "invhkey-1" || changes.delete[1] != "invhkey-2" {
		t.Errorf("delete must be ordered, got %v", changes.delete)
	}
}

// The mid-rotation state: two keys of ONE algorithm, told apart by their blob.
// A diff matching on (key_type) alone would read the incoming key as a change
// to the outgoing one and replace it.
func TestPlanChangesKeepsTwoKeysOfOneAlgorithmApart(t *testing.T) {
	changes := planChanges(
		[]hostKeyBlockModel{
			block("ssh-ed25519", edKey, "revoked"),
			block("ssh-ed25519", edKeyNew, ""),
		},
		[]terrapod.InventoryHostKey{row("invhkey-1", "ssh-ed25519", edKey, "")},
	)
	// The outgoing key is re-marked, the incoming one declared, and nothing is
	// withdrawn.
	if len(changes.create) != 1 || changes.create[0].PublicKey.ValueString() != edKeyNew {
		t.Errorf("the incoming key must be declared: %+v", changes.create)
	}
	if len(changes.remark) != 1 || changes.remark[0].marker != "revoked" {
		t.Errorf("the outgoing key must be re-marked: %+v", changes.remark)
	}
	if len(changes.delete) != 0 {
		t.Errorf("nothing is withdrawn mid-rotation: %v", changes.delete)
	}
}

// ── Read ─────────────────────────────────────────────────────────────────────

func TestReadIntoModelProjectsTheRows(t *testing.T) {
	m := &inventoryHostKeysModel{
		ID:     types.StringValue("invhost-5"),
		HostID: types.StringValue("invhost-5"),
	}
	diags := readIntoModel([]terrapod.InventoryHostKey{
		row("invhkey-1", "ssh-ed25519", edKey, ""),
		row("invhkey-2", "ssh-ed25519", edKeyNew, "revoked"),
	}, m)
	if diags.HasError() {
		t.Fatalf("read: %v", diags)
	}

	blocks, d := blocksOf(context.Background(), m.Keys)
	if d.HasError() {
		t.Fatalf("reading the set back: %v", d)
	}
	if len(blocks) != 2 {
		t.Fatalf("want two blocks, got %+v", blocks)
	}
	var sawNull, sawRevoked bool
	for _, b := range blocks {
		if b.Marker.IsNull() {
			sawNull = true
		}
		if b.Marker.ValueString() == "revoked" {
			sawRevoked = true
		}
	}
	// The unmarked row's marker is NULL, not "": an empty string would differ
	// from the configuration that omitted the attribute.
	if !sawNull {
		t.Error("an unmarked row must project a null marker")
	}
	if !sawRevoked {
		t.Error("a marked row must project its marker")
	}
}

func TestReadIntoModelProjectsAnEmptySet(t *testing.T) {
	m := &inventoryHostKeysModel{
		ID:     types.StringValue("invhost-5"),
		HostID: types.StringValue("invhost-5"),
	}
	if diags := readIntoModel(nil, m); diags.HasError() {
		t.Fatalf("read: %v", diags)
	}
	// An empty set, not null: the host exists and holds no approved key, which
	// is a different statement from "we do not know".
	if m.Keys.IsNull() || len(m.Keys.Elements()) != 0 {
		t.Errorf("want an empty set, got %v", m.Keys)
	}
	// And the host must be left alone -- with no rows there is nothing to
	// compare against, so a guess would be the only alternative.
	if m.HostID.ValueString() != "invhost-5" {
		t.Errorf("host_id = %q", m.HostID.ValueString())
	}
}

// A configuration naming the bare uuid keeps that form (#1748). `host_id`
// forces a replacement, so a drifted spelling would destroy and recreate the
// whole trust set.
func TestReadIntoModelKeepsTheConfiguredHostForm(t *testing.T) {
	rows := []terrapod.InventoryHostKey{row("invhkey-1", "ssh-ed25519", edKey, "")}
	rows[0].HostID = "invhost-9999"

	bare := &inventoryHostKeysModel{HostID: types.StringValue("9999")}
	if d := readIntoModel(rows, bare); d.HasError() {
		t.Fatal(d)
	}
	if bare.HostID.ValueString() != "9999" {
		t.Errorf("the configured bare form should be kept, got %q", bare.HostID.ValueString())
	}

	other := &inventoryHostKeysModel{HostID: types.StringValue("invhost-1")}
	if d := readIntoModel(rows, other); d.HasError() {
		t.Fatal(d)
	}
	if other.HostID.ValueString() != "invhost-9999" {
		t.Errorf("real drift must be reported, got %q", other.HostID.ValueString())
	}
}

// ── Driven through the resource ──────────────────────────────────────────────

func TestReadRemovesAVanishedHost(t *testing.T) {
	ctx := context.Background()
	r := &inventoryHostKeysResource{tc: fakeClient(t, notFound)}
	state := populated(t)
	resp := resource.ReadResponse{State: state}
	r.Read(ctx, resource.ReadRequest{State: state}, &resp)

	if resp.Diagnostics.HasError() {
		t.Fatalf("a 404 is not an error: %v", resp.Diagnostics)
	}
	// There is no trust-set object to go missing: the list route 404s once the
	// host is gone, and the host cascades its keys.
	if !resp.State.Raw.IsNull() {
		t.Error("a vanished host must remove the resource from state")
	}
}

func TestReadReportsAFailureThatIsNotAMissingHost(t *testing.T) {
	ctx := context.Background()
	r := &inventoryHostKeysResource{tc: fakeClient(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
	})}
	state := populated(t)
	resp := resource.ReadResponse{State: state}
	r.Read(ctx, resource.ReadRequest{State: state}, &resp)

	if !resp.Diagnostics.HasError() {
		t.Error("a 403 must be reported, not read as an empty trust set")
	}
	if resp.State.Raw.IsNull() {
		t.Error("a 403 must not remove the resource from state")
	}
}

// Read must address the host's keys route. A copy of a sibling resource that
// kept its call would read the wrong rows.
func TestReadAddressesTheHostKeysRoute(t *testing.T) {
	ctx := context.Background()
	var gotPath string
	r := &inventoryHostKeysResource{tc: fakeClient(t, func(w http.ResponseWriter, req *http.Request) {
		gotPath = req.URL.Path
		writeKeyList(w, row("invhkey-1", "ssh-ed25519", edKey, ""))
	})}
	state := populated(t)
	resp := resource.ReadResponse{State: state}
	r.Read(ctx, resource.ReadRequest{State: state}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("read: %v", resp.Diagnostics)
	}
	if gotPath != "/api/v1/inventory-hosts/invhost-5/keys" {
		t.Errorf("read must list the host's keys, got %q", gotPath)
	}
}

// The apply order is the security property: CREATE before DELETE, so the trust
// set is never momentarily empty and a rotation cannot leave a window in which
// the host presents a key nothing approves.
func TestUpdateCreatesBeforeItDeletes(t *testing.T) {
	ctx := context.Background()
	var calls []string
	r := &inventoryHostKeysResource{tc: fakeClient(t, func(w http.ResponseWriter, req *http.Request) {
		switch req.Method {
		case http.MethodGet:
			writeKeyList(w, row("invhkey-old", "ssh-ed25519", edKey, ""))
		case http.MethodPost:
			calls = append(calls, "create")
			w.Header().Set("Content-Type", "application/vnd.api+json")
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"invhkey-new","type":"inventory-host-keys",
			  "attributes":{"key-type":"ssh-ed25519","public-key":"x","marker":null}}}`))
		case http.MethodDelete:
			calls = append(calls, "delete")
			w.WriteHeader(http.StatusNoContent)
		default:
			calls = append(calls, strings.ToLower(req.Method))
			w.WriteHeader(http.StatusOK)
		}
	})}

	// The configuration replaces the key outright: one to declare, one to
	// withdraw.
	plan := stateWith(t, block("ssh-ed25519", edKeyNew, ""))
	resp := resource.UpdateResponse{State: nullState(t)}
	r.Update(ctx, resource.UpdateRequest{Plan: tfsdk.Plan(plan)}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("update: %v", resp.Diagnostics)
	}

	if len(calls) != 2 || calls[0] != "create" || calls[1] != "delete" {
		t.Errorf("calls = %v, want create then delete so the trust set is never empty", calls)
	}
}

// Destroy withdraws every key the host currently holds, not just the ones state
// remembers -- this resource owns the trust set, so a key added out of band
// must not survive it.
func TestDeleteWithdrawsEveryKeyTheHostHolds(t *testing.T) {
	ctx := context.Background()
	var deleted []string
	r := &inventoryHostKeysResource{tc: fakeClient(t, func(w http.ResponseWriter, req *http.Request) {
		if req.Method == http.MethodGet {
			writeKeyList(w,
				row("invhkey-1", "ssh-ed25519", edKey, ""),
				row("invhkey-2", "ssh-rsa", "AAAAB3NzaC1yc2EAAAA", ""))
			return
		}
		deleted = append(deleted, strings.TrimPrefix(req.URL.Path, "/api/v1/inventory-host-keys/"))
		w.WriteHeader(http.StatusNoContent)
	})}

	// State remembers ONE key; the host holds two.
	state := stateWith(t, block("ssh-ed25519", edKey, ""))
	resp := resource.DeleteResponse{State: state}
	r.Delete(ctx, resource.DeleteRequest{State: state}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("delete: %v", resp.Diagnostics)
	}
	if len(deleted) != 2 {
		t.Errorf("deleted = %v, want every key the host holds", deleted)
	}
}

func TestDeleteSwallowsAVanishedHost(t *testing.T) {
	ctx := context.Background()
	r := &inventoryHostKeysResource{tc: fakeClient(t, notFound)}
	state := populated(t)
	resp := resource.DeleteResponse{State: state}
	r.Delete(ctx, resource.DeleteRequest{State: state}, &resp)

	if resp.Diagnostics.HasError() {
		t.Fatalf("a host already gone took its keys with it: %v", resp.Diagnostics)
	}
}

func TestCreateDeclaresEveryBlock(t *testing.T) {
	ctx := context.Background()
	var sent []map[string]any
	r := &inventoryHostKeysResource{tc: fakeClient(t, func(w http.ResponseWriter, req *http.Request) {
		raw, _ := io.ReadAll(req.Body)
		var doc struct {
			Data struct {
				Attributes map[string]any `json:"attributes"`
			} `json:"data"`
		}
		if err := json.Unmarshal(raw, &doc); err == nil {
			sent = append(sent, doc.Data.Attributes)
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"data":{"id":"invhkey-1","type":"inventory-host-keys",
		  "attributes":{"key-type":"ssh-ed25519","public-key":"x","marker":null}}}`))
	})}

	plan := stateWith(t,
		block("ssh-ed25519", edKey, ""),
		block("ecdsa-sha2-nistp256", ecKey, "revoked"))
	resp := resource.CreateResponse{State: nullState(t)}
	r.Create(ctx, resource.CreateRequest{Plan: tfsdk.Plan(plan)}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("create: %v", resp.Diagnostics)
	}
	if len(sent) != 2 {
		t.Fatalf("want one POST per block, got %d", len(sent))
	}
	// One of the two carries the marker and the other omits it entirely.
	var withMarker, withoutMarker int
	for _, attrs := range sent {
		if _, ok := attrs["marker"]; ok {
			withMarker++
		} else {
			withoutMarker++
		}
	}
	if withMarker != 1 || withoutMarker != 1 {
		t.Errorf("markers sent = %d with, %d without: %+v", withMarker, withoutMarker, sent)
	}
	// `id` is the host, because the host IS this resource's identity.
	var id types.String
	if d := resp.State.GetAttribute(ctx, path.Root("id"), &id); d.HasError() {
		t.Fatalf("reading id: %v", d)
	}
	if id.ValueString() != "invhost-5" {
		t.Errorf("id = %q, want the host id", id.ValueString())
	}
}

func TestCreateReportsAFailureAndNamesTheKey(t *testing.T) {
	ctx := context.Background()
	r := &inventoryHostKeysResource{tc: fakeClient(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(`{"errors":[{"detail":"not base64","status":"422"}],"detail":"not base64"}`))
	})}

	plan := stateWith(t, block("ssh-ed25519", edKey, ""))
	resp := resource.CreateResponse{State: nullState(t)}
	r.Create(ctx, resource.CreateRequest{Plan: tfsdk.Plan(plan)}, &resp)

	if !resp.Diagnostics.HasError() {
		t.Fatal("a refused key must be reported")
	}
	// Which key, because a host declares several and "Create failed" alone
	// would not say which block to look at.
	if !strings.Contains(resp.Diagnostics.Errors()[0].Detail(), "ssh-ed25519") {
		t.Errorf("the error must name the key: %q", resp.Diagnostics.Errors()[0].Detail())
	}
}

// ── Helpers ──────────────────────────────────────────────────────────────────

func schemaOf(t *testing.T) rschema.Schema {
	t.Helper()
	var resp resource.SchemaResponse
	NewResource().Schema(context.Background(), resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema: %v", resp.Diagnostics)
	}
	return resp.Schema
}

func keyBlock(t *testing.T) rschema.SetNestedBlock {
	t.Helper()
	b, ok := schemaOf(t).Blocks["key"].(rschema.SetNestedBlock)
	if !ok {
		t.Fatalf("key is not a SetNestedBlock")
	}
	return b
}

func block(keyType, publicKey, marker string) hostKeyBlockModel {
	return hostKeyBlockModel{
		KeyType:   types.StringValue(keyType),
		PublicKey: types.StringValue(publicKey),
		Marker:    markerValue(marker),
	}
}

func row(id, keyType, publicKey, marker string) terrapod.InventoryHostKey {
	return terrapod.InventoryHostKey{
		ID:     id,
		HostID: "invhost-5",
		ApprovedHostKey: terrapod.ApprovedHostKey{
			KeyType:   keyType,
			PublicKey: publicKey,
			Marker:    marker,
		},
	}
}

func writeKeyList(w http.ResponseWriter, rows ...terrapod.InventoryHostKey) {
	parts := make([]string, 0, len(rows))
	for _, r := range rows {
		marker := "null"
		if r.Marker != "" {
			marker = fmt.Sprintf("%q", r.Marker)
		}
		parts = append(parts, fmt.Sprintf(`{"id":%q,"type":"inventory-host-keys",
		  "attributes":{"key-type":%q,"public-key":%q,"marker":%s},
		  "relationships":{"host":{"data":{"id":%q,"type":"inventory-hosts"}}}}`,
			r.ID, r.KeyType, r.PublicKey, marker, r.HostID))
	}
	w.Header().Set("Content-Type", "application/vnd.api+json")
	_, _ = fmt.Fprintf(w, `{"data":[%s],"meta":{"pagination":{"current-page":1,
	  "page-size":20,"total-count":%d,"total-pages":1}}}`,
		strings.Join(parts, ","), len(rows))
}

func notFound(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNotFound) }

func fakeClient(t *testing.T, h http.HandlerFunc) *terrapod.Client {
	t.Helper()
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: srv.URL, Token: "test-token"})
	if err != nil {
		t.Fatalf("building a client against the fake: %v", err)
	}
	return tc
}

func nullState(t *testing.T) tfsdk.State {
	t.Helper()
	ctx := context.Background()
	s := schemaOf(t)
	objType, ok := s.Type().TerraformType(ctx).(tftypes.Object)
	if !ok {
		t.Fatal("the schema's terraform type is not an object")
	}
	attrs := make(map[string]tftypes.Value, len(objType.AttributeTypes))
	for name, ty := range objType.AttributeTypes {
		attrs[name] = tftypes.NewValue(ty, nil)
	}
	return tfsdk.State{Schema: s, Raw: tftypes.NewValue(objType, attrs)}
}

// populated is a state holding one key.
func populated(t *testing.T) tfsdk.State {
	t.Helper()
	return stateWith(t, block("ssh-ed25519", edKey, ""))
}

func stateWith(t *testing.T, blocks ...hostKeyBlockModel) tfsdk.State {
	t.Helper()
	ctx := context.Background()
	elems := make([]attr.Value, 0, len(blocks))
	for _, b := range blocks {
		obj, d := types.ObjectValue(keyBlockType.AttrTypes, map[string]attr.Value{
			"key_type":   b.KeyType,
			"public_key": b.PublicKey,
			"marker":     b.Marker,
		})
		if d.HasError() {
			t.Fatalf("building a block: %v", d)
		}
		elems = append(elems, obj)
	}
	set, d := types.SetValue(keyBlockType, elems)
	if d.HasError() {
		t.Fatalf("building the set: %v", d)
	}

	st := nullState(t)
	if d := st.Set(ctx, &inventoryHostKeysModel{
		ID:     types.StringValue("invhost-5"),
		HostID: types.StringValue("invhost-5"),
		Keys:   set,
	}); d.HasError() {
		t.Fatalf("building state: %v", d)
	}
	return st
}
