package inventory_host

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

	// The server reports a group count and a variable count; this resource
	// must NOT carry them. They move when some OTHER resource creates a
	// membership or a variable, so a host holding them would report drift
	// because something else was declared correctly.
	for _, name := range []string{"group_count", "variable_count"} {
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

// workspace_id forces a replacement because a host belongs to one workspace;
// name does not, because the server renames in place. Replacing a host on a
// rename would briefly remove it from the target set.
func TestOnlyWorkspaceIDForcesReplacement(t *testing.T) {
	ctx := context.Background()
	var resp resource.SchemaResponse
	NewResource().Schema(ctx, resource.SchemaRequest{}, &resp)

	if !forcesReplacement(t, ctx, resp.Schema, "workspace_id") {
		t.Error("workspace_id must force a replacement: a host belongs to one workspace")
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

func TestImportIsByHostID(t *testing.T) {
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
	var got types.String
	if d := resp.State.GetAttribute(ctx, path.Root("id"), &got); d.HasError() {
		t.Fatalf("reading the imported id: %v", d)
	}
	if got.ValueString() != "invhost-abc" {
		t.Errorf("imported id = %q", got.ValueString())
	}
}

// ── The projection, both ways ────────────────────────────────────────────────

func TestReadIntoModelTakesTheServersValues(t *testing.T) {
	host := &terrapod.InventoryHost{
		ID:          "invhost-1",
		WorkspaceID: "ws-9",
		Name:        "web-1",
		CreatedAt:   "2026-10-07T10:00:00Z",
		UpdatedAt:   "2026-10-07T11:00:00Z",
	}
	m := &inventoryHostModel{WorkspaceID: types.StringValue("ws-9"), Name: types.StringValue("old")}
	readIntoModel(host, m)

	if m.ID.ValueString() != "invhost-1" {
		t.Errorf("id = %q", m.ID.ValueString())
	}
	// The server's name wins, which is how a rename made outside Terraform is
	// reported as drift.
	if m.Name.ValueString() != "web-1" {
		t.Errorf("name = %q", m.Name.ValueString())
	}
	if m.CreatedAt.ValueString() != "2026-10-07T10:00:00Z" ||
		m.UpdatedAt.ValueString() != "2026-10-07T11:00:00Z" {
		t.Errorf("timestamps = %v %v", m.CreatedAt, m.UpdatedAt)
	}
}

// A configuration naming the bare uuid keeps that form, so neither spelling
// drifts into a perpetual diff on an attribute that forces replacement (#1748).
func TestReadIntoModelKeepsTheConfiguredWorkspaceForm(t *testing.T) {
	host := &terrapod.InventoryHost{ID: "invhost-1", WorkspaceID: "ws-9999", Name: "web-1"}

	bare := &inventoryHostModel{WorkspaceID: types.StringValue("9999")}
	readIntoModel(host, bare)
	if bare.WorkspaceID.ValueString() != "9999" {
		t.Errorf("the bare configured id should be kept, got %q", bare.WorkspaceID.ValueString())
	}

	// An import starts with nothing, so the server's value is what makes the
	// imported host plan clean.
	imported := &inventoryHostModel{WorkspaceID: types.StringNull()}
	readIntoModel(host, imported)
	if imported.WorkspaceID.ValueString() != "ws-9999" {
		t.Errorf("an import must take the server's workspace, got %q", imported.WorkspaceID.ValueString())
	}

	// A genuinely different workspace is drift, so the server wins.
	other := &inventoryHostModel{WorkspaceID: types.StringValue("ws-1")}
	readIntoModel(host, other)
	if other.WorkspaceID.ValueString() != "ws-9999" {
		t.Errorf("a different workspace must take the server's value, got %q", other.WorkspaceID.ValueString())
	}
}

// ── Driven through the resource, not through its helpers ─────────────────────

// Read must drop the resource rather than erroring when the row is gone: a
// host deleted outside Terraform is a thing to recreate, not a failure.
func TestReadRemovesAVanishedHost(t *testing.T) {
	ctx := context.Background()
	r := &inventoryHostResource{tc: fakeClient(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	})}

	state := stateWith(t, &inventoryHostModel{
		ID:          types.StringValue("invhost-1"),
		WorkspaceID: types.StringValue("ws-9"),
		Name:        types.StringValue("web-1"),
		CreatedAt:   types.StringValue(""),
		UpdatedAt:   types.StringValue(""),
	})
	resp := resource.ReadResponse{State: state}
	r.Read(ctx, resource.ReadRequest{State: state}, &resp)

	if resp.Diagnostics.HasError() {
		t.Fatalf("a 404 is not an error: %v", resp.Diagnostics)
	}
	if !resp.State.Raw.IsNull() {
		t.Error("a vanished host must be removed from state")
	}
}

// Delete swallows a 404: something else already removed the row, which is the
// outcome Delete was asking for.
func TestDeleteSwallowsNotFound(t *testing.T) {
	ctx := context.Background()
	r := &inventoryHostResource{tc: fakeClient(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	})}

	state := stateWith(t, &inventoryHostModel{
		ID:          types.StringValue("invhost-1"),
		WorkspaceID: types.StringValue("ws-9"),
		Name:        types.StringValue("web-1"),
		CreatedAt:   types.StringValue(""),
		UpdatedAt:   types.StringValue(""),
	})
	resp := resource.DeleteResponse{State: state}
	r.Delete(ctx, resource.DeleteRequest{State: state}, &resp)

	if resp.Diagnostics.HasError() {
		t.Fatalf("a 404 on delete is not an error: %v", resp.Diagnostics)
	}
}

// Anything that is not a 404 IS an error. A 403 must not read as "the row is
// gone", which would silently drop a host from state while it still exists —
// and a lost permission is exactly the case where that would be wrong.
//
// A definitive 4xx on purpose: a 5xx is retried with backoff, so testing this
// branch through one would spend six seconds proving something about the retry
// instead.
func TestReadReportsAFailureThatIsNotAMissingRow(t *testing.T) {
	ctx := context.Background()
	r := &inventoryHostResource{tc: fakeClient(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
	})}

	state := stateWith(t, &inventoryHostModel{
		ID:          types.StringValue("invhost-1"),
		WorkspaceID: types.StringValue("ws-9"),
		Name:        types.StringValue("web-1"),
		CreatedAt:   types.StringValue(""),
		UpdatedAt:   types.StringValue(""),
	})
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

func TestHostNameValidatorRefusesWhatCannotBeTargeted(t *testing.T) {
	ctx := context.Background()
	for _, tc := range []struct {
		name    string
		value   string
		refused bool
	}{
		{"plain", "web-1", false},
		{"fqdn", "web-1.internal.example.invalid", false},
		{"ipv4", "10.0.0.4", false},
		{"empty", "", true},
		{"space", "web 1", true},
		{"tab", "web\t1", true},
		{"leading space", " web", true},
		{"comma", "web,db", true},
		{"colon", "web:1", true},
		{"excluding bang", "!web", true},
		{"ampersand", "web&db", true},
		{"tilde", "~web", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var resp validator.StringResponse
			hostNameValidator{}.ValidateString(ctx, validator.StringRequest{
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

func TestHostNameValidatorIgnoresUnknownAndNull(t *testing.T) {
	ctx := context.Background()
	for _, v := range []types.String{types.StringNull(), types.StringUnknown()} {
		var resp validator.StringResponse
		hostNameValidator{}.ValidateString(ctx, validator.StringRequest{
			Path: path.Root("name"), ConfigValue: v,
		}, &resp)
		if resp.Diagnostics.HasError() {
			t.Errorf("%v must not be validated: %v", v, resp.Diagnostics)
		}
	}
}

func TestBadCharsIsSortedAndDistinct(t *testing.T) {
	if got := badChars("web~:a:b~", forbiddenHostNameChars); got != ":~" {
		t.Errorf("badChars = %q, want \":~\"", got)
	}
	if got := badChars("web-1", forbiddenHostNameChars); got != "" {
		t.Errorf("badChars = %q, want \"\"", got)
	}
}

// ── Helpers ──────────────────────────────────────────────────────────────────

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

// nullState is the resource's own schema with every attribute null, which is
// what an import starts from.
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

func stateWith(t *testing.T, m *inventoryHostModel) tfsdk.State {
	t.Helper()
	st := nullState(t)
	if d := st.Set(context.Background(), m); d.HasError() {
		t.Fatalf("building state: %v", d)
	}
	return st
}
