package inventory_group_child

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
	for _, name := range []string{"id", "parent_group_id", "child_group_id", "created_at"} {
		if _, ok := attrs[name]; !ok {
			t.Errorf("attribute %q is missing", name)
		}
	}
	if len(attrs) != 4 {
		t.Errorf("unexpected attribute count %d: %v", len(attrs), attrs)
	}

	// A nesting never changes, so there is no updated_at — and therefore no
	// ModifyPlan to carry one across a no-change re-plan.
	if _, ok := attrs["updated_at"]; ok {
		t.Error("a nesting has no updated_at: it is immutable")
	}
	if _, ok := NewResource().(resource.ResourceWithModifyPlan); ok {
		t.Error("with no updated_at there is nothing for ModifyPlan to carry")
	}

	if !attrs["parent_group_id"].IsRequired() || !attrs["child_group_id"].IsRequired() {
		t.Error("both sides are required")
	}
	for _, name := range []string{"id", "created_at"} {
		if !attrs[name].IsComputed() || attrs[name].IsOptional() || attrs[name].IsRequired() {
			t.Errorf("%s must be computed-only", name)
		}
	}
}

func TestBothSidesForceReplacement(t *testing.T) {
	ctx := context.Background()
	var resp resource.SchemaResponse
	NewResource().Schema(ctx, resource.SchemaRequest{}, &resp)

	for _, name := range []string{"parent_group_id", "child_group_id"} {
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

func TestImportIsByNestingID(t *testing.T) {
	ctx := context.Background()
	r, ok := NewResource().(resource.ResourceWithImportState)
	if !ok {
		t.Fatal("the resource must support import")
	}
	resp := resource.ImportStateResponse{State: nullState(t)}
	r.ImportState(ctx, resource.ImportStateRequest{ID: "invgc-abc"}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("import: %v", resp.Diagnostics)
	}
	var got types.String
	if d := resp.State.GetAttribute(ctx, path.Root("id"), &got); d.HasError() {
		t.Fatalf("reading the imported id: %v", d)
	}
	if got.ValueString() != "invgc-abc" {
		t.Errorf("imported id = %q", got.ValueString())
	}
}

// ── The projection ───────────────────────────────────────────────────────────

func TestReadIntoModelTakesTheServersValues(t *testing.T) {
	gc := &terrapod.InventoryGroupChild{
		ID:            "invgc-1",
		ParentGroupID: "invgroup-5",
		ChildGroupID:  "invgroup-7",
		CreatedAt:     "2026-10-07T10:00:00Z",
	}
	m := &inventoryGroupChildModel{
		ParentGroupID: types.StringValue("invgroup-5"),
		ChildGroupID:  types.StringValue("invgroup-7"),
	}
	readIntoModel(gc, m)

	if m.ID.ValueString() != "invgc-1" || m.CreatedAt.ValueString() != "2026-10-07T10:00:00Z" {
		t.Errorf("model = %+v", m)
	}
}

// Both sides keep the form the configuration wrote. These attributes force a
// replacement, so a drifted spelling would destroy and recreate the
// nesting (#1748).
func TestReadIntoModelKeepsBothConfiguredIDForms(t *testing.T) {
	gc := &terrapod.InventoryGroupChild{
		ID: "invgc-1", ParentGroupID: "invgroup-5", ChildGroupID: "invgroup-7",
	}

	bare := &inventoryGroupChildModel{
		ParentGroupID: types.StringValue("5"),
		ChildGroupID:  types.StringValue("7"),
	}
	readIntoModel(gc, bare)
	if bare.ParentGroupID.ValueString() != "5" || bare.ChildGroupID.ValueString() != "7" {
		t.Errorf("the bare configured ids should be kept, got %+v", bare)
	}

	imported := &inventoryGroupChildModel{
		ParentGroupID: types.StringNull(), ChildGroupID: types.StringNull(),
	}
	readIntoModel(gc, imported)
	if imported.ParentGroupID.ValueString() != "invgroup-5" ||
		imported.ChildGroupID.ValueString() != "invgroup-7" {
		t.Errorf("an import must take the server's ids, got %+v", imported)
	}

	// The two sides must not be confused for each other: a model naming the
	// parent where the server has the child is drift, not a match.
	swapped := &inventoryGroupChildModel{
		ParentGroupID: types.StringValue("invgroup-7"),
		ChildGroupID:  types.StringValue("invgroup-5"),
	}
	readIntoModel(gc, swapped)
	if swapped.ParentGroupID.ValueString() != "invgroup-5" ||
		swapped.ChildGroupID.ValueString() != "invgroup-7" {
		t.Errorf("a swapped pair must take the server's values, got %+v", swapped)
	}
}

// ── Driven through the resource ──────────────────────────────────────────────

// Create goes through the PARENT's side, so one resource has one code path.
// What that means on the wire: the path names the parent and the body carries
// the child.
func TestCreateGoesThroughTheParentsSide(t *testing.T) {
	ctx := context.Background()
	var gotPath string
	var gotRel string
	r := &inventoryGroupChildResource{tc: fakeClient(t, func(w http.ResponseWriter, req *http.Request) {
		gotPath = req.URL.Path
		raw, _ := io.ReadAll(req.Body)
		var body struct {
			Data struct {
				Relationships map[string]json.RawMessage `json:"relationships"`
			} `json:"data"`
		}
		if err := json.Unmarshal(raw, &body); err != nil {
			t.Errorf("create body is not JSON: %v", err)
		}
		for name := range body.Data.Relationships {
			gotRel = name
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(`{"data":{"id":"invgc-1","type":"inventory-group-children",
			"attributes":{"created-at":"2026-10-07T10:00:00Z"},
			"relationships":{"parent-group":{"data":{"id":"invgroup-5","type":"inventory-groups"}},
			"child-group":{"data":{"id":"invgroup-7","type":"inventory-groups"}}}}}`))
	})}

	plan := planWith(t, &inventoryGroupChildModel{
		ParentGroupID: types.StringValue("invgroup-5"),
		ChildGroupID:  types.StringValue("invgroup-7"),
	})
	resp := resource.CreateResponse{State: nullState(t)}
	r.Create(ctx, resource.CreateRequest{Plan: plan}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("create: %v", resp.Diagnostics)
	}

	if !strings.Contains(gotPath, "/inventory-groups/invgroup-5/children") {
		t.Errorf("create must address the parent's side, got path %q", gotPath)
	}
	if gotRel != "child-group" {
		t.Errorf("the body must carry the child, got relationship %q", gotRel)
	}
}

func TestReadRemovesAVanishedNesting(t *testing.T) {
	ctx := context.Background()
	r := &inventoryGroupChildResource{tc: fakeClient(t, notFound)}
	state := populated(t)
	resp := resource.ReadResponse{State: state}
	r.Read(ctx, resource.ReadRequest{State: state}, &resp)

	if resp.Diagnostics.HasError() {
		t.Fatalf("a 404 is not an error: %v", resp.Diagnostics)
	}
	if !resp.State.Raw.IsNull() {
		t.Error("a vanished nesting must be removed from state")
	}
}

func TestDeleteSwallowsNotFound(t *testing.T) {
	ctx := context.Background()
	r := &inventoryGroupChildResource{tc: fakeClient(t, notFound)}
	state := populated(t)
	resp := resource.DeleteResponse{State: state}
	r.Delete(ctx, resource.DeleteRequest{State: state}, &resp)

	if resp.Diagnostics.HasError() {
		t.Fatalf("a 404 on delete is not an error: %v", resp.Diagnostics)
	}
}

func TestReadReportsAFailureThatIsNotAMissingRow(t *testing.T) {
	ctx := context.Background()
	r := &inventoryGroupChildResource{tc: fakeClient(t, func(w http.ResponseWriter, _ *http.Request) {
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
	r := &inventoryGroupChildResource{}
	resp := resource.UpdateResponse{State: nullState(t)}
	r.Update(ctx, resource.UpdateRequest{}, &resp)
	if !resp.Diagnostics.HasError() {
		t.Error("a nesting is immutable: Update must refuse")
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
	d := st.Set(context.Background(), &inventoryGroupChildModel{
		ID:            types.StringValue("invgc-1"),
		ParentGroupID: types.StringValue("invgroup-5"),
		ChildGroupID:  types.StringValue("invgroup-7"),
		CreatedAt:     types.StringValue(""),
	})
	if d.HasError() {
		t.Fatalf("building state: %v", d)
	}
	return st
}

func planWith(t *testing.T, m *inventoryGroupChildModel) tfsdk.Plan {
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
