package inventory_settings

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
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/defaults"
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
		"id", "workspace_id", "include_platform", "vcs_connection_id",
		"repo_url", "branch", "working_directory", "ignore_paths",
		"created_at", "updated_at",
	} {
		if _, ok := attrs[name]; !ok {
			t.Errorf("attribute %q is missing", name)
		}
	}
	if len(attrs) != 10 {
		t.Errorf("unexpected attribute count %d: %v", len(attrs), attrs)
	}

	if !attrs["workspace_id"].IsRequired() {
		t.Error("workspace_id is required: it is the key")
	}
	// Optional and NOT computed: removing one of these from the configuration
	// has to mean "clear it", which Computed + UseStateForUnknown would turn
	// into "keep what you have".
	for _, name := range []string{
		"vcs_connection_id", "repo_url", "branch", "working_directory", "ignore_paths",
	} {
		a := attrs[name]
		if !a.IsOptional() {
			t.Errorf("%s must be optional", name)
		}
		if a.IsComputed() {
			t.Errorf("%s must NOT be computed: a removed attribute could then never clear it", name)
		}
	}
	for _, name := range []string{"id", "created_at", "updated_at"} {
		if !attrs[name].IsComputed() || attrs[name].IsOptional() || attrs[name].IsRequired() {
			t.Errorf("%s must be computed-only", name)
		}
	}
	if got := attrs["ignore_paths"].GetType().String(); got != "types.ListType[basetypes.StringType]" {
		t.Errorf("ignore_paths type = %s", got)
	}
}

// include_platform defaults to TRUE, which is what a workspace with no
// settings resource at all already does. Defaulting it false would mean that
// adding this resource purely to bind a VCS directory silently stopped the
// declared hosts, groups and variables being used.
func TestIncludePlatformDefaultsToTrue(t *testing.T) {
	ctx := context.Background()
	var resp resource.SchemaResponse
	NewResource().Schema(ctx, resource.SchemaRequest{}, &resp)

	a, ok := resp.Schema.Attributes["include_platform"].(rschema.BoolAttribute)
	if !ok {
		t.Fatal("include_platform is not a BoolAttribute")
	}
	if a.Default == nil {
		t.Fatal("include_platform must carry a default: a PUT has to send a bool either way")
	}

	var defResp defaults.BoolResponse
	a.Default.DefaultBool(ctx, defaults.BoolRequest{}, &defResp)
	if !defResp.PlanValue.ValueBool() {
		t.Error("include_platform must default to true, not to false")
	}
}

func TestOnlyWorkspaceIDForcesReplacement(t *testing.T) {
	ctx := context.Background()
	var resp resource.SchemaResponse
	NewResource().Schema(ctx, resource.SchemaRequest{}, &resp)

	if !forcesReplacement(t, ctx, resp.Schema, "workspace_id") {
		t.Error("workspace_id must force a replacement: it is the key")
	}
	for _, name := range []string{"repo_url", "branch", "working_directory", "vcs_connection_id"} {
		if forcesReplacement(t, ctx, resp.Schema, name) {
			t.Errorf("%s must NOT force a replacement: a PUT replaces it in place", name)
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

// Import takes the workspace id, because the settings id IS the workspace id —
// there is one inventory per workspace, so there is no surrogate key to import
// by and no composite to parse.
func TestImportSetsBothTheIDAndTheWorkspace(t *testing.T) {
	ctx := context.Background()
	r, ok := NewResource().(resource.ResourceWithImportState)
	if !ok {
		t.Fatal("the resource must support import")
	}
	resp := resource.ImportStateResponse{State: nullState(t)}
	r.ImportState(ctx, resource.ImportStateRequest{ID: "ws-abc"}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("import: %v", resp.Diagnostics)
	}
	for _, attr := range []string{"id", "workspace_id"} {
		var got types.String
		if d := resp.State.GetAttribute(ctx, path.Root(attr), &got); d.HasError() {
			t.Fatalf("reading the imported %s: %v", attr, d)
		}
		if got.ValueString() != "ws-abc" {
			t.Errorf("imported %s = %q", attr, got.ValueString())
		}
	}
}

// ── The PUT request ──────────────────────────────────────────────────────────

// Every field is sent, including the empty ones. PUT replaces, so an omitted
// field would take its default rather than keeping the stored value — which is
// exactly what clearing an attribute in the configuration should do.
func TestBuildPutRequestSendsTheEmptyFormForARemovedAttribute(t *testing.T) {
	ctx := context.Background()
	req, d := buildPutRequest(ctx, &inventorySettingsModel{
		IncludePlatform:  types.BoolValue(true),
		VCSConnectionID:  types.StringNull(),
		RepoURL:          types.StringNull(),
		Branch:           types.StringNull(),
		WorkingDirectory: types.StringNull(),
		IgnorePaths:      types.ListNull(types.StringType),
	})
	if d.HasError() {
		t.Fatal(d)
	}
	if req.VCSConnectionID != "" || req.RepoURL != "" || req.Branch != "" || req.WorkingDirectory != "" {
		t.Errorf("a removed string must be sent as \"\" to clear it: %+v", req)
	}
	// Not nil: the SDK turns a nil slice into `[]` for a PUT, but a caller
	// reading this request should see the clearing form plainly.
	if req.IgnorePaths == nil || len(req.IgnorePaths) != 0 {
		t.Errorf("a removed list must be sent as an empty slice, got %v", req.IgnorePaths)
	}
	if !req.IncludePlatform {
		t.Error("include_platform must carry through")
	}
}

func TestBuildPutRequestSendsWhatWasConfigured(t *testing.T) {
	ctx := context.Background()
	paths, d := types.ListValueFrom(ctx, types.StringType, []string{"scratch", "README.md"})
	if d.HasError() {
		t.Fatal(d)
	}
	req, d := buildPutRequest(ctx, &inventorySettingsModel{
		IncludePlatform:  types.BoolValue(false),
		VCSConnectionID:  types.StringValue("vcs-7"),
		RepoURL:          types.StringValue("https://git.example.invalid/org/repo"),
		Branch:           types.StringValue("main"),
		WorkingDirectory: types.StringValue("inventory"),
		IgnorePaths:      paths,
	})
	if d.HasError() {
		t.Fatal(d)
	}
	if req.IncludePlatform {
		t.Error("include_platform = false must carry through")
	}
	if req.VCSConnectionID != "vcs-7" || req.Branch != "main" || req.WorkingDirectory != "inventory" {
		t.Errorf("request = %+v", req)
	}
	if len(req.IgnorePaths) != 2 || req.IgnorePaths[0] != "scratch" {
		t.Errorf("ignore paths = %v", req.IgnorePaths)
	}
}

// ── The read ─────────────────────────────────────────────────────────────────

// An optional attribute the configuration left out stays null when the server
// holds nothing for it. The server renders an unset string as "" and an unset
// list as absent; storing those over a null config value fails the apply with
// "Provider produced inconsistent result after apply".
func TestReadIntoModelKeepsNullForTheServersEmptyValues(t *testing.T) {
	ctx := context.Background()
	s := &terrapod.InventorySettings{
		ID:              "ws-9",
		WorkspaceID:     "ws-9",
		IncludePlatform: true,
		CreatedAt:       "2026-10-07T10:00:00Z",
		UpdatedAt:       "2026-10-07T10:00:00Z",
	}
	m := &inventorySettingsModel{
		WorkspaceID:      types.StringValue("ws-9"),
		VCSConnectionID:  types.StringNull(),
		RepoURL:          types.StringNull(),
		Branch:           types.StringNull(),
		WorkingDirectory: types.StringNull(),
		IgnorePaths:      types.ListNull(types.StringType),
	}
	if d := readIntoModel(ctx, s, m); d.HasError() {
		t.Fatal(d)
	}
	if !m.VCSConnectionID.IsNull() || !m.RepoURL.IsNull() || !m.Branch.IsNull() ||
		!m.WorkingDirectory.IsNull() || !m.IgnorePaths.IsNull() {
		t.Errorf("a null config must stay null when the server holds nothing: %+v", m)
	}
	if m.ID.ValueString() != "ws-9" || !m.IncludePlatform.ValueBool() {
		t.Errorf("model = %+v", m)
	}
}

// A configured `ignore_paths = []` must round-trip as `[]` rather than
// becoming null, or the apply fails as "inconsistent result after apply".
func TestReadIntoModelKeepsAConfiguredEmptyList(t *testing.T) {
	ctx := context.Background()
	empty, d := types.ListValueFrom(ctx, types.StringType, []string{})
	if d.HasError() {
		t.Fatal(d)
	}
	s := &terrapod.InventorySettings{ID: "ws-9", WorkspaceID: "ws-9"}
	m := &inventorySettingsModel{
		WorkspaceID: types.StringValue("ws-9"),
		RepoURL:     types.StringValue(""),
		IgnorePaths: empty,
	}
	if d := readIntoModel(ctx, s, m); d.HasError() {
		t.Fatal(d)
	}
	if m.IgnorePaths.IsNull() || len(m.IgnorePaths.Elements()) != 0 {
		t.Errorf("a configured empty list must stay an empty list, got %v", m.IgnorePaths)
	}
	if m.RepoURL.IsNull() || m.RepoURL.ValueString() != "" {
		t.Errorf("a configured empty repo_url must stay \"\", got %v", m.RepoURL)
	}
}

func TestReadIntoModelTakesTheServersValues(t *testing.T) {
	ctx := context.Background()
	s := &terrapod.InventorySettings{
		ID:               "ws-9",
		WorkspaceID:      "ws-9",
		IncludePlatform:  false,
		VCSConnectionID:  "vcs-7",
		RepoURL:          "https://git.example.invalid/org/repo",
		Branch:           "main",
		WorkingDirectory: "inventory",
		IgnorePaths:      []string{"scratch"},
	}
	m := &inventorySettingsModel{
		WorkspaceID: types.StringValue("ws-9"),
		IgnorePaths: types.ListNull(types.StringType),
	}
	if d := readIntoModel(ctx, s, m); d.HasError() {
		t.Fatal(d)
	}
	if m.IncludePlatform.ValueBool() {
		t.Error("include_platform must take the server's false")
	}
	if m.VCSConnectionID.ValueString() != "vcs-7" || m.Branch.ValueString() != "main" {
		t.Errorf("model = %+v", m)
	}
	if len(m.IgnorePaths.Elements()) != 1 {
		t.Errorf("ignore paths = %v", m.IgnorePaths)
	}
}

// Both id attributes keep the form the configuration wrote whenever it names
// what the server returned (#1748) — and a data source's `.id`, the obvious
// thing to interpolate, is prefixed while an endpoint may answer bare.
func TestReadIntoModelKeepsTheConfiguredIDForms(t *testing.T) {
	ctx := context.Background()
	s := &terrapod.InventorySettings{ID: "ws-9999", WorkspaceID: "ws-9999", VCSConnectionID: "vcs-77"}

	bare := &inventorySettingsModel{
		WorkspaceID:     types.StringValue("9999"),
		VCSConnectionID: types.StringValue("77"),
		IgnorePaths:     types.ListNull(types.StringType),
	}
	if d := readIntoModel(ctx, s, bare); d.HasError() {
		t.Fatal(d)
	}
	if bare.WorkspaceID.ValueString() != "9999" {
		t.Errorf("the bare workspace id should be kept, got %q", bare.WorkspaceID.ValueString())
	}
	if bare.VCSConnectionID.ValueString() != "77" {
		t.Errorf("the bare connection id should be kept, got %q", bare.VCSConnectionID.ValueString())
	}

	imported := &inventorySettingsModel{
		WorkspaceID:     types.StringNull(),
		VCSConnectionID: types.StringNull(),
		IgnorePaths:     types.ListNull(types.StringType),
	}
	if d := readIntoModel(ctx, s, imported); d.HasError() {
		t.Fatal(d)
	}
	if imported.WorkspaceID.ValueString() != "ws-9999" ||
		imported.VCSConnectionID.ValueString() != "vcs-77" {
		t.Errorf("an import must take the server's ids, got %+v", imported)
	}
}

// A connection cleared server-side must read as null, not as "", or a
// configuration that never set one would be handed an empty string.
func TestReadIntoModelLeavesAnUnboundConnectionNull(t *testing.T) {
	ctx := context.Background()
	s := &terrapod.InventorySettings{ID: "ws-9", WorkspaceID: "ws-9", VCSConnectionID: ""}
	m := &inventorySettingsModel{
		WorkspaceID:     types.StringValue("ws-9"),
		VCSConnectionID: types.StringNull(),
		IgnorePaths:     types.ListNull(types.StringType),
	}
	if d := readIntoModel(ctx, s, m); d.HasError() {
		t.Fatal(d)
	}
	if !m.VCSConnectionID.IsNull() {
		t.Errorf("an unbound connection must stay null, got %v", m.VCSConnectionID)
	}
}

// ── Driven through the resource ──────────────────────────────────────────────

// Read addresses the workspace, not an id of its own, because the settings row
// is keyed on its workspace.
func TestReadAddressesTheWorkspacesSettings(t *testing.T) {
	ctx := context.Background()
	var gotPath string
	r := &inventorySettingsResource{tc: fakeClient(t, func(w http.ResponseWriter, req *http.Request) {
		gotPath = req.URL.Path
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(`{"data":{"id":"ws-9","type":"inventory-settings",
			"attributes":{"include-platform":true},
			"relationships":{"workspace":{"data":{"id":"ws-9","type":"workspaces"}}}}}`))
	})}

	state := populated(t)
	resp := resource.ReadResponse{State: state}
	r.Read(ctx, resource.ReadRequest{State: state}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("read: %v", resp.Diagnostics)
	}
	if gotPath != "/api/v1/workspaces/ws-9/inventory/settings" {
		t.Errorf("read must address the workspace's settings, got %q", gotPath)
	}
}

// Update PUTs rather than PATCHes: a full replace is the only shape that can
// CLEAR a field the configuration no longer sets, and Terraform always knows
// the whole intended state.
func TestUpdatePutsRatherThanPatches(t *testing.T) {
	ctx := context.Background()
	var gotMethod string
	var gotAttrs map[string]any
	r := &inventorySettingsResource{tc: fakeClient(t, func(w http.ResponseWriter, req *http.Request) {
		gotMethod = req.Method
		raw, _ := io.ReadAll(req.Body)
		var body struct {
			Data struct {
				Attributes map[string]any `json:"attributes"`
			} `json:"data"`
		}
		_ = json.Unmarshal(raw, &body)
		gotAttrs = body.Data.Attributes
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(`{"data":{"id":"ws-9","type":"inventory-settings",
			"attributes":{"include-platform":true},
			"relationships":{"workspace":{"data":{"id":"ws-9","type":"workspaces"}}}}}`))
	})}

	plan := planWith(t, &inventorySettingsModel{
		ID:              types.StringValue("ws-9"),
		WorkspaceID:     types.StringValue("ws-9"),
		IncludePlatform: types.BoolValue(true),
		IgnorePaths:     types.ListNull(types.StringType),
	})
	resp := resource.UpdateResponse{State: populated(t)}
	r.Update(ctx, resource.UpdateRequest{Plan: plan}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("update: %v", resp.Diagnostics)
	}
	if gotMethod != http.MethodPut {
		t.Errorf("update must PUT (a full replace), got %s", gotMethod)
	}
	// A PATCH omits what it is not changing; a PUT must carry every field, so
	// the cleared ones are present and empty.
	for _, key := range []string{"repo-url", "branch", "working-directory", "ignore-paths"} {
		if _, ok := gotAttrs[key]; !ok {
			t.Errorf("a full replace must send %q even when empty", key)
		}
	}
}

// A workspace with no settings row is the normal default state, so a 404 means
// this resource's row is gone rather than that the read failed.
func TestReadRemovesVanishedSettings(t *testing.T) {
	ctx := context.Background()
	r := &inventorySettingsResource{tc: fakeClient(t, notFound)}
	state := populated(t)
	resp := resource.ReadResponse{State: state}
	r.Read(ctx, resource.ReadRequest{State: state}, &resp)

	if resp.Diagnostics.HasError() {
		t.Fatalf("a 404 is not an error: %v", resp.Diagnostics)
	}
	if !resp.State.Raw.IsNull() {
		t.Error("vanished settings must be removed from state")
	}
}

func TestDeleteSwallowsNotFound(t *testing.T) {
	ctx := context.Background()
	r := &inventorySettingsResource{tc: fakeClient(t, notFound)}
	state := populated(t)
	resp := resource.DeleteResponse{State: state}
	r.Delete(ctx, resource.DeleteRequest{State: state}, &resp)

	if resp.Diagnostics.HasError() {
		t.Fatalf("a 404 on delete is not an error: %v", resp.Diagnostics)
	}
}

func TestReadReportsAFailureThatIsNotAMissingRow(t *testing.T) {
	ctx := context.Background()
	r := &inventorySettingsResource{tc: fakeClient(t, func(w http.ResponseWriter, _ *http.Request) {
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
	d := st.Set(context.Background(), &inventorySettingsModel{
		ID:              types.StringValue("ws-9"),
		WorkspaceID:     types.StringValue("ws-9"),
		IncludePlatform: types.BoolValue(true),
		IgnorePaths:     types.ListNull(types.StringType),
		CreatedAt:       types.StringValue(""),
		UpdatedAt:       types.StringValue(""),
	})
	if d.HasError() {
		t.Fatalf("building state: %v", d)
	}
	return st
}

func planWith(t *testing.T, m *inventorySettingsModel) tfsdk.Plan {
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
