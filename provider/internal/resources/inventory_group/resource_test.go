package inventory_group

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	rschema "github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/schema/validator"
	"github.com/hashicorp/terraform-plugin-framework/tfsdk"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-go/tftypes"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// ── Schema ───────────────────────────────────────────────────────────────────

func TestSchemaRequirednessAndReplacement(t *testing.T) {
	ctx := context.Background()
	var resp resource.SchemaResponse
	NewResource().Schema(ctx, resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema: %v", resp.Diagnostics)
	}

	attrs := resp.Schema.Attributes
	for _, name := range []string{"id", "workspace_id", "name", "created_at", "updated_at"} {
		if _, ok := attrs[name]; !ok {
			t.Errorf("attribute %q is missing", name)
		}
	}
	if len(attrs) != 5 {
		t.Errorf("unexpected attribute count %d: %v", len(attrs), attrs)
	}

	// The server reports member, child and variable counts; this resource must
	// NOT carry them. They move when some OTHER resource creates a membership,
	// a nesting or a variable, so a group holding them would report drift
	// because something else was declared correctly.
	for _, name := range []string{"member_count", "child_count", "variable_count"} {
		if _, ok := attrs[name]; ok {
			t.Errorf("%q must not be an attribute: it moves when another resource is declared", name)
		}
	}

	if !attrs["workspace_id"].IsRequired() || !attrs["name"].IsRequired() {
		t.Error("workspace_id and name are required")
	}
	for _, name := range []string{"id", "created_at", "updated_at"} {
		if !attrs[name].IsComputed() || attrs[name].IsOptional() || attrs[name].IsRequired() {
			t.Errorf("%s must be computed-only", name)
		}
	}
}

// Renaming a group must not replace it: the memberships and nestings that
// point at the group are rows of their own and survive an in-place rename,
// where a replacement would cascade them all away.
func TestOnlyWorkspaceIDForcesReplacement(t *testing.T) {
	ctx := context.Background()
	var resp resource.SchemaResponse
	NewResource().Schema(ctx, resource.SchemaRequest{}, &resp)

	if !forcesReplacement(t, ctx, resp.Schema, "workspace_id") {
		t.Error("workspace_id must force a replacement: a group belongs to one workspace")
	}
	if forcesReplacement(t, ctx, resp.Schema, "name") {
		t.Error("name must NOT force a replacement: the server renames in place")
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

func TestImportIsByGroupID(t *testing.T) {
	ctx := context.Background()
	r, ok := NewResource().(resource.ResourceWithImportState)
	if !ok {
		t.Fatal("the resource must support import")
	}
	resp := resource.ImportStateResponse{State: nullState(t)}
	r.ImportState(ctx, resource.ImportStateRequest{ID: "invgroup-abc"}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("import: %v", resp.Diagnostics)
	}
	var got types.String
	if d := resp.State.GetAttribute(ctx, path.Root("id"), &got); d.HasError() {
		t.Fatalf("reading the imported id: %v", d)
	}
	if got.ValueString() != "invgroup-abc" {
		t.Errorf("imported id = %q", got.ValueString())
	}
}

// ── The projection, both ways ────────────────────────────────────────────────

func TestReadIntoModelTakesTheServersValues(t *testing.T) {
	group := &terrapod.InventoryGroup{
		ID:          "invgroup-1",
		WorkspaceID: "ws-9",
		Name:        "web",
		CreatedAt:   "2026-10-07T10:00:00Z",
		UpdatedAt:   "2026-10-07T11:00:00Z",
	}
	m := &inventoryGroupModel{WorkspaceID: types.StringValue("ws-9"), Name: types.StringValue("old")}
	readIntoModel(group, m)

	if m.ID.ValueString() != "invgroup-1" || m.Name.ValueString() != "web" {
		t.Errorf("model = %+v", m)
	}
	if m.CreatedAt.ValueString() != "2026-10-07T10:00:00Z" ||
		m.UpdatedAt.ValueString() != "2026-10-07T11:00:00Z" {
		t.Errorf("timestamps = %v %v", m.CreatedAt, m.UpdatedAt)
	}
}

// A configuration naming the bare uuid keeps that form, so neither spelling
// drifts into a perpetual diff on an attribute that forces replacement (#1748).
func TestReadIntoModelKeepsTheConfiguredWorkspaceForm(t *testing.T) {
	group := &terrapod.InventoryGroup{ID: "invgroup-1", WorkspaceID: "ws-9999", Name: "web"}

	bare := &inventoryGroupModel{WorkspaceID: types.StringValue("9999")}
	readIntoModel(group, bare)
	if bare.WorkspaceID.ValueString() != "9999" {
		t.Errorf("the bare configured id should be kept, got %q", bare.WorkspaceID.ValueString())
	}

	imported := &inventoryGroupModel{WorkspaceID: types.StringNull()}
	readIntoModel(group, imported)
	if imported.WorkspaceID.ValueString() != "ws-9999" {
		t.Errorf("an import must take the server's workspace, got %q", imported.WorkspaceID.ValueString())
	}

	other := &inventoryGroupModel{WorkspaceID: types.StringValue("ws-1")}
	readIntoModel(group, other)
	if other.WorkspaceID.ValueString() != "ws-9999" {
		t.Errorf("a different workspace must take the server's value, got %q", other.WorkspaceID.ValueString())
	}
}

// ── Driven through the resource, not through its helpers ─────────────────────

func TestReadRemovesAVanishedGroup(t *testing.T) {
	ctx := context.Background()
	r := &inventoryGroupResource{tc: fakeClient(t, notFound)}
	state := populated(t)
	resp := resource.ReadResponse{State: state}
	r.Read(ctx, resource.ReadRequest{State: state}, &resp)

	if resp.Diagnostics.HasError() {
		t.Fatalf("a 404 is not an error: %v", resp.Diagnostics)
	}
	if !resp.State.Raw.IsNull() {
		t.Error("a vanished group must be removed from state")
	}
}

func TestDeleteSwallowsNotFound(t *testing.T) {
	ctx := context.Background()
	r := &inventoryGroupResource{tc: fakeClient(t, notFound)}
	state := populated(t)
	resp := resource.DeleteResponse{State: state}
	r.Delete(ctx, resource.DeleteRequest{State: state}, &resp)

	if resp.Diagnostics.HasError() {
		t.Fatalf("a 404 on delete is not an error: %v", resp.Diagnostics)
	}
}

// Anything that is not a 404 IS an error. A definitive 4xx on purpose: a 5xx
// is retried with backoff, so testing this branch through one would spend
// seconds proving something about the retry instead.
func TestReadReportsAFailureThatIsNotAMissingRow(t *testing.T) {
	ctx := context.Background()
	r := &inventoryGroupResource{tc: fakeClient(t, func(w http.ResponseWriter, _ *http.Request) {
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

// ── Plan-time validation ─────────────────────────────────────────────────────

func TestGroupNameValidatorRefusesDerivedAndNonIdentifierNames(t *testing.T) {
	ctx := context.Background()
	for _, tc := range []struct {
		name    string
		value   string
		refused bool
	}{
		{"identifier", "web", false},
		{"underscored", "eu_west", false},
		{"leading underscore", "_staging", false},
		{"trailing digit", "db2", false},
		{"all is derived", "all", true},
		{"ungrouped is derived", "ungrouped", true},
		{"hyphen", "eu-west", true},
		{"leading digit", "2web", true},
		{"dot", "web.eu", true},
		{"empty", "", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var resp validator.StringResponse
			groupNameValidator{}.ValidateString(ctx, validator.StringRequest{
				Path:        path.Root("name"),
				ConfigValue: types.StringValue(tc.value),
			}, &resp)
			if resp.Diagnostics.HasError() != tc.refused {
				t.Errorf("%q refused=%t, want %t: %v",
					tc.value, resp.Diagnostics.HasError(), tc.refused, resp.Diagnostics)
			}
		})
	}
}

// The `all` refusal must point at the resource that DOES declare a variable
// applying to every host, or the practitioner is told what they cannot do and
// not what they can.
func TestTheAllRefusalNamesTheGlobalVarResource(t *testing.T) {
	msg := groupNameProblem("all")
	if !strings.Contains(msg, "terrapod_inventory_global_var") {
		t.Errorf("the refusal must name terrapod_inventory_global_var: %q", msg)
	}
}

func TestGroupNameValidatorIgnoresUnknownAndNull(t *testing.T) {
	ctx := context.Background()
	for _, v := range []types.String{types.StringNull(), types.StringUnknown()} {
		var resp validator.StringResponse
		groupNameValidator{}.ValidateString(ctx, validator.StringRequest{
			Path: path.Root("name"), ConfigValue: v,
		}, &resp)
		if resp.Diagnostics.HasError() {
			t.Errorf("%v must not be validated: %v", v, resp.Diagnostics)
		}
	}
}

// ── Helpers ──────────────────────────────────────────────────────────────────

func notFound(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNotFound) }

// fakeClient points a real go-terrapod client at a handler, so a test drives
// the resource's own CRUD path rather than only its projection helpers.
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
	d := st.Set(context.Background(), &inventoryGroupModel{
		ID:          types.StringValue("invgroup-1"),
		WorkspaceID: types.StringValue("ws-9"),
		Name:        types.StringValue("web"),
		CreatedAt:   types.StringValue(""),
		UpdatedAt:   types.StringValue(""),
	})
	if d.HasError() {
		t.Fatalf("building state: %v", d)
	}
	return st
}
