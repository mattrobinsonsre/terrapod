package inventory_host_group

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	rschema "github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/tfsdk"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-go/tftypes"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// ── Schema ───────────────────────────────────────────────────────────────────

func TestSchemaIsAnAddressableJoin(t *testing.T) {
	ctx := context.Background()
	var resp resource.SchemaResponse
	NewResource().Schema(ctx, resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema: %v", resp.Diagnostics)
	}

	attrs := resp.Schema.Attributes
	for _, name := range []string{"id", "host_id", "group_id", "created_at"} {
		if _, ok := attrs[name]; !ok {
			t.Errorf("attribute %q is missing", name)
		}
	}
	if len(attrs) != 4 {
		t.Errorf("unexpected attribute count %d: %v", len(attrs), attrs)
	}

	// A membership never changes, so there is no updated_at — and therefore no
	// ModifyPlan to carry one across a no-change re-plan.
	if _, ok := attrs["updated_at"]; ok {
		t.Error("a membership has no updated_at: it is immutable")
	}
	if _, ok := NewResource().(resource.ResourceWithModifyPlan); ok {
		t.Error("with no updated_at there is nothing for ModifyPlan to carry")
	}

	if !attrs["host_id"].IsRequired() || !attrs["group_id"].IsRequired() {
		t.Error("both sides are required")
	}
	for _, name := range []string{"id", "created_at"} {
		if !attrs[name].IsComputed() || attrs[name].IsOptional() || attrs[name].IsRequired() {
			t.Errorf("%s must be computed-only", name)
		}
	}
}

// Both sides force a replacement: there is nothing to change about a
// membership that is not a different membership.
func TestBothSidesForceReplacement(t *testing.T) {
	ctx := context.Background()
	var resp resource.SchemaResponse
	NewResource().Schema(ctx, resource.SchemaRequest{}, &resp)

	for _, name := range []string{"host_id", "group_id"} {
		if !forcesReplacement(t, ctx, resp.Schema, name) {
			t.Errorf("%s must force a replacement", name)
		}
	}
}

func forcesReplacement(t *testing.T, ctx context.Context, s rschema.Schema, name string) bool {
	t.Helper()
	a, ok := s.Attributes[name].(rschema.StringAttribute)
	if !ok {
		t.Fatalf("%s is not a StringAttribute", name)
	}
	for _, pm := range a.PlanModifiers {
		if strings.Contains(pm.Description(ctx), "destroy and recreate") {
			return true
		}
	}
	return false
}

// Import is passthrough on the membership id, not a composite of the two
// sides: the row is addressable in its own right, so both sides come back from
// the read.
func TestImportIsByMembershipID(t *testing.T) {
	ctx := context.Background()
	r, ok := NewResource().(resource.ResourceWithImportState)
	if !ok {
		t.Fatal("the resource must support import")
	}
	resp := resource.ImportStateResponse{State: nullState(t)}
	r.ImportState(ctx, resource.ImportStateRequest{ID: "invhg-abc"}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("import: %v", resp.Diagnostics)
	}
	var got types.String
	if d := resp.State.GetAttribute(ctx, path.Root("id"), &got); d.HasError() {
		t.Fatalf("reading the imported id: %v", d)
	}
	if got.ValueString() != "invhg-abc" {
		t.Errorf("imported id = %q", got.ValueString())
	}
}

// ── The projection ───────────────────────────────────────────────────────────

func TestReadIntoModelTakesTheServersValues(t *testing.T) {
	hg := &terrapod.InventoryHostGroup{
		ID:        "invhg-1",
		HostID:    "invhost-5",
		GroupID:   "invgroup-7",
		CreatedAt: "2026-10-07T10:00:00Z",
	}
	m := &inventoryHostGroupModel{
		HostID:  types.StringValue("invhost-5"),
		GroupID: types.StringValue("invgroup-7"),
	}
	readIntoModel(hg, m)

	if m.ID.ValueString() != "invhg-1" || m.CreatedAt.ValueString() != "2026-10-07T10:00:00Z" {
		t.Errorf("model = %+v", m)
	}
}

// Both sides keep the form the configuration wrote. These attributes force a
// replacement, so a drifted spelling would destroy and recreate the
// membership (#1748).
func TestReadIntoModelKeepsBothConfiguredIDForms(t *testing.T) {
	hg := &terrapod.InventoryHostGroup{ID: "invhg-1", HostID: "invhost-5", GroupID: "invgroup-7"}

	bare := &inventoryHostGroupModel{
		HostID:  types.StringValue("5"),
		GroupID: types.StringValue("7"),
	}
	readIntoModel(hg, bare)
	if bare.HostID.ValueString() != "5" {
		t.Errorf("the bare host id should be kept, got %q", bare.HostID.ValueString())
	}
	if bare.GroupID.ValueString() != "7" {
		t.Errorf("the bare group id should be kept, got %q", bare.GroupID.ValueString())
	}

	imported := &inventoryHostGroupModel{HostID: types.StringNull(), GroupID: types.StringNull()}
	readIntoModel(hg, imported)
	if imported.HostID.ValueString() != "invhost-5" || imported.GroupID.ValueString() != "invgroup-7" {
		t.Errorf("an import must take the server's ids, got %+v", imported)
	}

	other := &inventoryHostGroupModel{
		HostID:  types.StringValue("invhost-9"),
		GroupID: types.StringValue("invgroup-9"),
	}
	readIntoModel(hg, other)
	if other.HostID.ValueString() != "invhost-5" || other.GroupID.ValueString() != "invgroup-7" {
		t.Errorf("different ids must take the server's values, got %+v", other)
	}
}

// ── Driven through the resource ──────────────────────────────────────────────

// Create goes through the GROUP's side, so one resource has one code path.
// What that means on the wire: the path names the group and the body carries
// the host.
func TestCreateGoesThroughTheGroupsSide(t *testing.T) {
	ctx := context.Background()
	var gotPath string
	var gotRelType string
	r := &inventoryHostGroupResource{tc: fakeClient(t, func(w http.ResponseWriter, req *http.Request) {
		gotPath = req.URL.Path
		raw, _ := io.ReadAll(req.Body)
		var body struct {
			Data struct {
				Relationships map[string]struct {
					Data struct {
						Type string `json:"type"`
						ID   string `json:"id"`
					} `json:"data"`
				} `json:"relationships"`
			} `json:"data"`
		}
		if err := json.Unmarshal(raw, &body); err != nil {
			t.Errorf("create body is not JSON: %v", err)
		}
		for _, rel := range body.Data.Relationships {
			gotRelType = rel.Data.Type
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(`{"data":{"id":"invhg-1","type":"inventory-host-groups",
			"attributes":{"created-at":"2026-10-07T10:00:00Z"},
			"relationships":{"host":{"data":{"id":"invhost-5","type":"inventory-hosts"}},
			"group":{"data":{"id":"invgroup-7","type":"inventory-groups"}}}}}`))
	})}

	plan := planWith(t, &inventoryHostGroupModel{
		HostID:  types.StringValue("invhost-5"),
		GroupID: types.StringValue("invgroup-7"),
	})
	resp := resource.CreateResponse{State: nullState(t)}
	r.Create(ctx, resource.CreateRequest{Plan: plan}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("create: %v", resp.Diagnostics)
	}

	if !strings.Contains(gotPath, "/inventory-groups/invgroup-7/hosts") {
		t.Errorf("create must address the group's side, got path %q", gotPath)
	}
	if gotRelType != "inventory-hosts" {
		t.Errorf("the body must carry the host, got relationship type %q", gotRelType)
	}
}

func TestReadRemovesAVanishedMembership(t *testing.T) {
	ctx := context.Background()
	r := &inventoryHostGroupResource{tc: fakeClient(t, notFound)}
	state := populated(t)
	resp := resource.ReadResponse{State: state}
	r.Read(ctx, resource.ReadRequest{State: state}, &resp)

	if resp.Diagnostics.HasError() {
		t.Fatalf("a 404 is not an error: %v", resp.Diagnostics)
	}
	if !resp.State.Raw.IsNull() {
		t.Error("a vanished membership must be removed from state")
	}
}

func TestDeleteSwallowsNotFound(t *testing.T) {
	ctx := context.Background()
	r := &inventoryHostGroupResource{tc: fakeClient(t, notFound)}
	state := populated(t)
	resp := resource.DeleteResponse{State: state}
	r.Delete(ctx, resource.DeleteRequest{State: state}, &resp)

	if resp.Diagnostics.HasError() {
		t.Fatalf("a 404 on delete is not an error: %v", resp.Diagnostics)
	}
}

func TestReadReportsAFailureThatIsNotAMissingRow(t *testing.T) {
	ctx := context.Background()
	r := &inventoryHostGroupResource{tc: fakeClient(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
	})}
	state := populated(t)
	resp := resource.ReadResponse{State: state}
	r.Read(ctx, resource.ReadRequest{State: state}, &resp)

	if !resp.Diagnostics.HasError() {
		t.Error("a 403 must be reported, not treated as a missing row")
	}
	if resp.State.Raw.IsNull() {
		t.Error("a 403 must not remove the resource from state")
	}
}

// Update is unreachable because both sides force a replacement. It refuses
// rather than silently doing nothing, so that removing a RequiresReplace by
// accident shows up as an error instead of as a successful no-op.
func TestUpdateRefuses(t *testing.T) {
	ctx := context.Background()
	r := &inventoryHostGroupResource{}
	resp := resource.UpdateResponse{State: nullState(t)}
	r.Update(ctx, resource.UpdateRequest{}, &resp)
	if !resp.Diagnostics.HasError() {
		t.Error("a membership is immutable: Update must refuse")
	}
}

// ── Helpers ──────────────────────────────────────────────────────────────────

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
	var resp resource.SchemaResponse
	NewResource().Schema(ctx, resource.SchemaRequest{}, &resp)
	objType, ok := resp.Schema.Type().TerraformType(ctx).(tftypes.Object)
	if !ok {
		t.Fatal("the schema's terraform type is not an object")
	}
	attrs := make(map[string]tftypes.Value, len(objType.AttributeTypes))
	for name, ty := range objType.AttributeTypes {
		attrs[name] = tftypes.NewValue(ty, nil)
	}
	return tfsdk.State{Schema: resp.Schema, Raw: tftypes.NewValue(objType, attrs)}
}

func populated(t *testing.T) tfsdk.State {
	t.Helper()
	st := nullState(t)
	d := st.Set(context.Background(), &inventoryHostGroupModel{
		ID:        types.StringValue("invhg-1"),
		HostID:    types.StringValue("invhost-5"),
		GroupID:   types.StringValue("invgroup-7"),
		CreatedAt: types.StringValue(""),
	})
	if d.HasError() {
		t.Fatalf("building state: %v", d)
	}
	return st
}

func planWith(t *testing.T, m *inventoryHostGroupModel) tfsdk.Plan {
	t.Helper()
	ctx := context.Background()
	var resp resource.SchemaResponse
	NewResource().Schema(ctx, resource.SchemaRequest{}, &resp)
	objType := resp.Schema.Type().TerraformType(ctx).(tftypes.Object)
	attrs := make(map[string]tftypes.Value, len(objType.AttributeTypes))
	for name, ty := range objType.AttributeTypes {
		attrs[name] = tftypes.NewValue(ty, nil)
	}
	plan := tfsdk.Plan{Schema: resp.Schema, Raw: tftypes.NewValue(objType, attrs)}
	if d := plan.Set(ctx, m); d.HasError() {
		t.Fatalf("building plan: %v", d)
	}
	return plan
}
