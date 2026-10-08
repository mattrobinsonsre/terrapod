package inventory_global_var

import (
	"context"
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

func TestSchemaRequirednessAndReplacement(t *testing.T) {
	ctx := context.Background()
	var resp resource.SchemaResponse
	NewResource().Schema(ctx, resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema: %v", resp.Diagnostics)
	}

	attrs := resp.Schema.Attributes
	for _, name := range []string{
		"id", "workspace_id", "key", "value", "structured", "sensitive", "created_at", "updated_at",
	} {
		if _, ok := attrs[name]; !ok {
			t.Errorf("attribute %q is missing", name)
		}
	}
	if len(attrs) != 8 {
		t.Errorf("unexpected attribute count %d: %v", len(attrs), attrs)
	}

	if !attrs["workspace_id"].IsRequired() || !attrs["key"].IsRequired() || !attrs["value"].IsRequired() {
		t.Error("workspace_id, key and value are required")
	}
	// Redacted in plan output unconditionally: Terraform cannot vary an
	// attribute's sensitivity per row, and a become password in an ordinary
	// variable under `all` is exactly what this protects.
	if !attrs["value"].IsSensitive() {
		t.Error("value must be schema-sensitive: the sensitivity cannot be decided per row")
	}
	for _, name := range []string{"structured", "sensitive"} {
		a := attrs[name]
		if !a.IsOptional() || !a.IsComputed() {
			t.Errorf("%s must be optional + computed so its default applies", name)
		}
	}
	for _, name := range []string{"id", "created_at", "updated_at"} {
		if !attrs[name].IsComputed() || attrs[name].IsOptional() || attrs[name].IsRequired() {
			t.Errorf("%s must be computed-only", name)
		}
	}
}

// `key` is renameable: the SDK's update request carries it as a pointer and
// the server renames in place. Replacing on a rename would delete the variable
// and declare a new one, which for an inventory is a value that briefly is not
// set.
func TestOnlyTheParentForcesReplacement(t *testing.T) {
	ctx := context.Background()
	var resp resource.SchemaResponse
	NewResource().Schema(ctx, resource.SchemaRequest{}, &resp)

	if !forcesReplacement(t, ctx, resp.Schema, "workspace_id") {
		t.Error("workspace_id must force a replacement: a variable belongs to one workspace")
	}
	if forcesReplacement(t, ctx, resp.Schema, "key") {
		t.Error("key must NOT force a replacement: the server renames in place")
	}
	if forcesReplacement(t, ctx, resp.Schema, "value") {
		t.Error("value must NOT force a replacement")
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

func TestImportIsByVariableID(t *testing.T) {
	ctx := context.Background()
	r, ok := NewResource().(resource.ResourceWithImportState)
	if !ok {
		t.Fatal("the resource must support import")
	}
	resp := resource.ImportStateResponse{State: nullState(t)}
	r.ImportState(ctx, resource.ImportStateRequest{ID: "invvar-abc"}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("import: %v", resp.Diagnostics)
	}
	var got types.String
	if d := resp.State.GetAttribute(ctx, path.Root("id"), &got); d.HasError() {
		t.Fatalf("reading the imported id: %v", d)
	}
	if got.ValueString() != "invvar-abc" {
		t.Errorf("imported id = %q", got.ValueString())
	}
}

// ── The request builders ─────────────────────────────────────────────────────

func TestBuildCreateRequestSendsEveryField(t *testing.T) {
	req := buildCreateRequest(&inventoryGlobalVarModel{
		Key:        types.StringValue("ansible_user"),
		Value:      types.StringValue("deploy"),
		Structured: types.BoolValue(false),
		Sensitive:  types.BoolValue(true),
	})
	if req.Key != "ansible_user" || req.Value != "deploy" || req.Structured || !req.Sensitive {
		t.Errorf("request = %+v", req)
	}
}

// Every field is sent as a non-nil pointer. The SDK's pointers draw "leave
// alone" apart from "set", and Terraform always knows the whole intended
// state — so omitting one would make it impossible to, say, flip `sensitive`
// back to false and restore the value the configuration holds in one apply.
func TestBuildUpdateRequestSendsEveryFieldExplicitly(t *testing.T) {
	req := buildUpdateRequest(&inventoryGlobalVarModel{
		Key:        types.StringValue("ansible_port"),
		Value:      types.StringValue("2222"),
		Structured: types.BoolValue(true),
		Sensitive:  types.BoolValue(false),
	})
	if req.Key == nil || req.Value == nil || req.Structured == nil || req.Sensitive == nil {
		t.Fatalf("every field must be sent explicitly: %+v", req)
	}
	if *req.Key != "ansible_port" || *req.Value != "2222" || !*req.Structured || *req.Sensitive {
		t.Errorf("request = key=%q value=%q structured=%t sensitive=%t",
			*req.Key, *req.Value, *req.Structured, *req.Sensitive)
	}
}

// ── Read, including the value that never comes back ──────────────────────────

func TestReadIntoModelTakesTheServersValues(t *testing.T) {
	v := &terrapod.InventoryVar{
		ID:          "invvar-1",
		WorkspaceID: "ws-5",
		Key:         "ansible_user",
		Value:       "deploy",
		Structured:  true,
		Sensitive:   false,
		CreatedAt:   "2026-10-07T10:00:00Z",
		UpdatedAt:   "2026-10-07T11:00:00Z",
	}
	m := &inventoryGlobalVarModel{
		WorkspaceID: types.StringValue("ws-5"),
		Key:         types.StringValue("old_name"),
		Value:       types.StringValue("old"),
	}
	readIntoModel(v, m)

	if m.ID.ValueString() != "invvar-1" {
		t.Errorf("id = %q", m.ID.ValueString())
	}
	// A non-sensitive value reads back, so the server's answer wins and drift
	// is reported.
	if m.Key.ValueString() != "ansible_user" || m.Value.ValueString() != "deploy" {
		t.Errorf("key/value = %q %q", m.Key.ValueString(), m.Value.ValueString())
	}
	if !m.Structured.ValueBool() || m.Sensitive.ValueBool() {
		t.Errorf("flags = structured %v sensitive %v", m.Structured, m.Sensitive)
	}
}

// A sensitive variable reads back as the mask. Taking it would store "***" as
// the secret and then plan an update from "***" to the configured value on
// every run, which never converges.
func TestReadIntoModelKeepsTheConfiguredValueWhenTheServerMasksIt(t *testing.T) {
	v := &terrapod.InventoryVar{
		ID:          "invvar-1",
		WorkspaceID: "ws-5",
		Key:         "ansible_become_pass",
		Value:       terrapod.MaskedValue,
		Sensitive:   true,
	}
	m := &inventoryGlobalVarModel{
		WorkspaceID: types.StringValue("ws-5"),
		Value:       types.StringValue("s3cr3t"),
	}
	readIntoModel(v, m)

	if m.Value.ValueString() != "s3cr3t" {
		t.Errorf("a masked read must keep the configured value, got %q", m.Value.ValueString())
	}
	if !m.Sensitive.ValueBool() {
		t.Error("the sensitive flag itself does read back and must be taken from the server")
	}
}

// An import has no prior value, so it takes the mask. That is the honest
// answer — nothing can recover a value the server will not return — and the
// next plan correctly shows the configured value replacing it.
func TestReadIntoModelTakesTheMaskWhenThereIsNoPriorValue(t *testing.T) {
	v := &terrapod.InventoryVar{
		ID: "invvar-1", WorkspaceID: "ws-5", Value: terrapod.MaskedValue, Sensitive: true,
	}
	for _, prior := range []types.String{types.StringNull(), types.StringUnknown()} {
		m := &inventoryGlobalVarModel{WorkspaceID: types.StringValue("ws-5"), Value: prior}
		readIntoModel(v, m)
		if m.Value.ValueString() != terrapod.MaskedValue {
			t.Errorf("with prior %v the mask is the honest answer, got %q", prior, m.Value.ValueString())
		}
	}
}

// The mask rule must not swallow a genuine change to a non-sensitive value
// that happens to be "***" — the state keeps it either way, which is the same
// answer, but a real value moving must still be reported.
func TestReadIntoModelReportsARealValueChange(t *testing.T) {
	v := &terrapod.InventoryVar{ID: "invvar-1", WorkspaceID: "ws-5", Value: "changed"}
	m := &inventoryGlobalVarModel{
		WorkspaceID: types.StringValue("ws-5"),
		Value:       types.StringValue("original"),
	}
	readIntoModel(v, m)
	if m.Value.ValueString() != "changed" {
		t.Errorf("a readable value must be taken from the server, got %q", m.Value.ValueString())
	}
}

// A configuration naming the bare uuid keeps that form (#1748). This attribute
// forces a replacement, so a drifted spelling would destroy and recreate the
// variable.
func TestReadIntoModelKeepsTheConfiguredParentForm(t *testing.T) {
	v := &terrapod.InventoryVar{ID: "invvar-1", WorkspaceID: "ws-9999", Key: "k", Value: "x"}

	bare := &inventoryGlobalVarModel{WorkspaceID: types.StringValue("9999")}
	readIntoModel(v, bare)
	if bare.WorkspaceID.ValueString() != "9999" {
		t.Errorf("the bare configured id should be kept, got %q", bare.WorkspaceID.ValueString())
	}

	imported := &inventoryGlobalVarModel{WorkspaceID: types.StringNull()}
	readIntoModel(v, imported)
	if imported.WorkspaceID.ValueString() != "ws-9999" {
		t.Errorf("an import must take the server's workspace, got %q", imported.WorkspaceID.ValueString())
	}

	other := &inventoryGlobalVarModel{WorkspaceID: types.StringValue("ws-1")}
	readIntoModel(v, other)
	if other.WorkspaceID.ValueString() != "ws-9999" {
		t.Errorf("a different workspace must take the server's value, got %q", other.WorkspaceID.ValueString())
	}
}

// ── Driven through the resource ──────────────────────────────────────────────

func TestReadRemovesAVanishedVariable(t *testing.T) {
	ctx := context.Background()
	r := &inventoryGlobalVarResource{tc: fakeClient(t, notFound)}
	state := populated(t)
	resp := resource.ReadResponse{State: state}
	r.Read(ctx, resource.ReadRequest{State: state}, &resp)

	if resp.Diagnostics.HasError() {
		t.Fatalf("a 404 is not an error: %v", resp.Diagnostics)
	}
	if !resp.State.Raw.IsNull() {
		t.Error("a vanished variable must be removed from state")
	}
}

func TestDeleteSwallowsNotFound(t *testing.T) {
	ctx := context.Background()
	r := &inventoryGlobalVarResource{tc: fakeClient(t, notFound)}
	state := populated(t)
	resp := resource.DeleteResponse{State: state}
	r.Delete(ctx, resource.DeleteRequest{State: state}, &resp)

	if resp.Diagnostics.HasError() {
		t.Fatalf("a 404 on delete is not an error: %v", resp.Diagnostics)
	}
}

func TestReadReportsAFailureThatIsNotAMissingRow(t *testing.T) {
	ctx := context.Background()
	r := &inventoryGlobalVarResource{tc: fakeClient(t, func(w http.ResponseWriter, _ *http.Request) {
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

// Read must address the global-variable route, not the host or group one: the
// three share a struct and differ only in their routes, so a copied resource
// that kept a sibling's call would read the wrong row.
func TestReadAddressesTheOwnVarRoute(t *testing.T) {
	ctx := context.Background()
	var gotPath string
	r := &inventoryGlobalVarResource{tc: fakeClient(t, func(w http.ResponseWriter, req *http.Request) {
		gotPath = req.URL.Path
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(`{"data":{"id":"invvar-1","type":"inventory-global-vars",
			"attributes":{"key":"k","value":"v","structured":false,"sensitive":false},
			"relationships":{"workspace":{"data":{"id":"ws-5","type":"workspaces"}}}}}`))
	})}

	state := populated(t)
	resp := resource.ReadResponse{State: state}
	r.Read(ctx, resource.ReadRequest{State: state}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("read: %v", resp.Diagnostics)
	}
	if gotPath != "/api/v1/inventory-global-vars/invvar-1" {
		t.Errorf("read must address the global-variable route, got %q", gotPath)
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
	d := st.Set(context.Background(), &inventoryGlobalVarModel{
		ID:          types.StringValue("invvar-1"),
		WorkspaceID: types.StringValue("ws-5"),
		Key:         types.StringValue("ansible_user"),
		Value:       types.StringValue("deploy"),
		Structured:  types.BoolValue(false),
		Sensitive:   types.BoolValue(false),
		CreatedAt:   types.StringValue(""),
		UpdatedAt:   types.StringValue(""),
	})
	if d.HasError() {
		t.Fatalf("building state: %v", d)
	}
	return st
}
