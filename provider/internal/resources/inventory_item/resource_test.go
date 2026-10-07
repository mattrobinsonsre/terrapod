package inventory_item

import (
	"context"
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

func setOf(t *testing.T, values ...string) types.Set {
	t.Helper()
	if values == nil {
		// A nil slice builds a NULL set, which is not what "no elements" means
		// here — the empty and null forms are exactly what several of these
		// tests exist to tell apart.
		values = []string{}
	}
	sv, d := types.SetValueFrom(context.Background(), types.StringType, values)
	if d.HasError() {
		t.Fatalf("building a set of %v: %v", values, d)
	}
	return sv
}

func mapOf(t *testing.T, kv map[string]string) types.Map {
	t.Helper()
	mv, d := types.MapValueFrom(context.Background(), types.StringType, kv)
	if d.HasError() {
		t.Fatalf("building a map of %v: %v", kv, d)
	}
	return mv
}

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
		"id", "workspace_id", "name", "address", "groups", "vars", "created_at", "updated_at",
	} {
		if _, ok := attrs[name]; !ok {
			t.Errorf("attribute %q is missing", name)
		}
	}
	if len(attrs) != 8 {
		t.Errorf("unexpected attribute count %d: %v", len(attrs), attrs)
	}

	if !attrs["workspace_id"].IsRequired() || !attrs["name"].IsRequired() {
		t.Error("workspace_id and name are required")
	}
	// Optional and NOT computed: removing the attribute has to mean "clear it",
	// which Computed + UseStateForUnknown would turn into "keep what you have".
	for _, name := range []string{"address", "groups", "vars"} {
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
	if got := attrs["groups"].GetType().String(); got != "types.SetType[basetypes.StringType]" {
		t.Errorf("groups type = %s", got)
	}
	if got := attrs["vars"].GetType().String(); got != "types.MapType[basetypes.StringType]" {
		t.Errorf("vars type = %s", got)
	}
}

// workspace_id forces a replacement because an item belongs to one workspace;
// name does not, because the server renames in place and answers 409 on a
// collision. Replacing a host on a rename would delete it and declare a new
// one, which for an inventory is a target set that briefly does not hold it.
func TestOnlyWorkspaceIDForcesReplacement(t *testing.T) {
	ctx := context.Background()
	var resp resource.SchemaResponse
	NewResource().Schema(ctx, resource.SchemaRequest{}, &resp)

	replaces := func(name string) bool {
		a, ok := resp.Schema.Attributes[name].(rschema.StringAttribute)
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

	if !replaces("workspace_id") {
		t.Error("workspace_id must force a replacement: an item belongs to one workspace")
	}
	if replaces("name") {
		t.Error("name must NOT force a replacement: the server renames in place")
	}
}

func TestImportIsByItemID(t *testing.T) {
	ctx := context.Background()
	r, ok := NewResource().(resource.ResourceWithImportState)
	if !ok {
		t.Fatal("the resource must support import")
	}
	resp := resource.ImportStateResponse{State: nullState(t)}
	r.ImportState(ctx, resource.ImportStateRequest{ID: "invitem-abc"}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("import: %v", resp.Diagnostics)
	}
	var got types.String
	if d := resp.State.GetAttribute(ctx, path.Root("id"), &got); d.HasError() {
		t.Fatalf("reading the imported id: %v", d)
	}
	if got.ValueString() != "invitem-abc" {
		t.Errorf("imported id = %q", got.ValueString())
	}
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

// ── Create ───────────────────────────────────────────────────────────────────

func TestBuildCreateRequestOmitsUnconfiguredCollections(t *testing.T) {
	ctx := context.Background()
	m := &inventoryItemModel{
		Name:    types.StringValue("web-1"),
		Address: types.StringNull(),
		Groups:  types.SetNull(types.StringType),
		Vars:    types.MapNull(types.StringType),
	}
	req, d := buildCreateRequest(ctx, m)
	if d.HasError() {
		t.Fatal(d)
	}
	if req.Name != "web-1" || req.Address != "" {
		t.Errorf("request = %+v", req)
	}
	if req.Groups != nil || req.Vars != nil {
		t.Errorf("unconfigured collections must be omitted on create: %+v %+v", req.Groups, req.Vars)
	}
}

func TestBuildCreateRequestSendsWhatWasConfigured(t *testing.T) {
	ctx := context.Background()
	m := &inventoryItemModel{
		Name:    types.StringValue("web-1"),
		Address: types.StringValue("10.0.0.4"),
		Groups:  setOf(t, "web", "eu_west"),
		Vars:    mapOf(t, map[string]string{"ansible_user": "deploy"}),
	}
	req, d := buildCreateRequest(ctx, m)
	if d.HasError() {
		t.Fatal(d)
	}
	if req.Address != "10.0.0.4" || len(req.Groups) != 2 || req.Vars["ansible_user"] != "deploy" {
		t.Errorf("request = %+v", req)
	}
}

// ── Update: the clearing form ────────────────────────────────────────────────

// Removing `groups` from the configuration must send the CLEARING form (a
// pointer to an empty slice, which the API reads as `[]`), not omit the
// attribute. The SDK's pointers exist to tell those two apart, and omission is
// "leave the host's groups alone" -- so collapsing them would make a host
// impossible to remove from its groups.
func TestBuildUpdateRequestClearsARemovedGroupsAttribute(t *testing.T) {
	ctx := context.Background()
	m := &inventoryItemModel{
		Name:    types.StringValue("web-1"),
		Address: types.StringNull(),
		Groups:  types.SetNull(types.StringType),
		Vars:    types.MapNull(types.StringType),
	}
	req, d := buildUpdateRequest(ctx, m)
	if d.HasError() {
		t.Fatal(d)
	}
	if req.Groups == nil {
		t.Fatal("a removed groups attribute must be sent as the clearing form, not omitted")
	}
	if len(*req.Groups) != 0 {
		t.Errorf("the clearing form is an empty slice, got %v", *req.Groups)
	}
	if req.Vars == nil {
		t.Fatal("a removed vars attribute must be sent as the clearing form, not omitted")
	}
	if len(*req.Vars) != 0 {
		t.Errorf("the clearing form is an empty map, got %v", *req.Vars)
	}
	if req.Address == nil || *req.Address != "" {
		t.Errorf("a removed address must be sent as \"\" to clear it, got %v", req.Address)
	}
}

// The SDK turns a pointer-to-nil-slice into `[]` as well, but it must be given
// a pointer in the first place -- so what this pins is that the pointer is
// never nil, however the empty value is spelled.
func TestBuildUpdateRequestClearingFormSurvivesTheSDK(t *testing.T) {
	ctx := context.Background()
	m := &inventoryItemModel{
		Name:   types.StringValue("web-1"),
		Groups: types.SetNull(types.StringType),
		Vars:   types.MapNull(types.StringType),
	}
	req, _ := buildUpdateRequest(ctx, m)
	if req.Groups == nil || req.Vars == nil {
		t.Fatal("the SDK cannot clear what it was not given a pointer to")
	}
}

func TestBuildUpdateRequestSendsConfiguredValues(t *testing.T) {
	ctx := context.Background()
	m := &inventoryItemModel{
		Name:    types.StringValue("web-2"),
		Address: types.StringValue("10.0.0.5"),
		Groups:  setOf(t, "web"),
		Vars:    mapOf(t, map[string]string{"tier": "front"}),
	}
	req, d := buildUpdateRequest(ctx, m)
	if d.HasError() {
		t.Fatal(d)
	}
	if req.Name != "web-2" {
		t.Errorf("name = %q", req.Name)
	}
	if req.Address == nil || *req.Address != "10.0.0.5" {
		t.Errorf("address = %v", req.Address)
	}
	if req.Groups == nil || len(*req.Groups) != 1 || (*req.Groups)[0] != "web" {
		t.Errorf("groups = %v", req.Groups)
	}
	if req.Vars == nil || (*req.Vars)["tier"] != "front" {
		t.Errorf("vars = %v", req.Vars)
	}
}

// An unknown value is not a request to clear anything.
func TestBuildUpdateRequestOmitsUnknownAttributes(t *testing.T) {
	ctx := context.Background()
	m := &inventoryItemModel{
		Name:    types.StringValue("web-1"),
		Address: types.StringUnknown(),
		Groups:  types.SetUnknown(types.StringType),
		Vars:    types.MapUnknown(types.StringType),
	}
	req, d := buildUpdateRequest(ctx, m)
	if d.HasError() {
		t.Fatal(d)
	}
	if req.Address != nil || req.Groups != nil || req.Vars != nil {
		t.Errorf("an unknown value must be omitted, not cleared: %+v", req)
	}
}

// ── Read ─────────────────────────────────────────────────────────────────────

func TestReadIntoModelKeepsNullForTheServersEmptyValues(t *testing.T) {
	ctx := context.Background()
	item := &terrapod.InventoryItem{
		ID:          "invitem-1",
		WorkspaceID: "ws-9",
		Name:        "web-1",
		CreatedAt:   "2026-10-07T10:00:00Z",
		UpdatedAt:   "2026-10-07T10:00:00Z",
	}
	m := &inventoryItemModel{
		WorkspaceID: types.StringValue("ws-9"),
		Address:     types.StringNull(),
		Groups:      types.SetNull(types.StringType),
		Vars:        types.MapNull(types.StringType),
	}
	if d := readIntoModel(ctx, item, m); d.HasError() {
		t.Fatal(d)
	}
	if !m.Address.IsNull() || !m.Groups.IsNull() || !m.Vars.IsNull() {
		t.Errorf("a null config must stay null when the server holds nothing: %+v", m)
	}
	if m.ID.ValueString() != "invitem-1" || m.Name.ValueString() != "web-1" {
		t.Errorf("model = %+v", m)
	}
}

// A configured `groups = []` must round-trip as `[]` rather than becoming null,
// or the apply fails as "Provider produced inconsistent result after apply".
func TestReadIntoModelKeepsAConfiguredEmptyCollection(t *testing.T) {
	ctx := context.Background()
	item := &terrapod.InventoryItem{ID: "invitem-1", WorkspaceID: "ws-9", Name: "web-1"}
	m := &inventoryItemModel{
		WorkspaceID: types.StringValue("ws-9"),
		Address:     types.StringValue(""),
		Groups:      setOf(t),
		Vars:        mapOf(t, map[string]string{}),
	}
	if d := readIntoModel(ctx, item, m); d.HasError() {
		t.Fatal(d)
	}
	if m.Groups.IsNull() || len(m.Groups.Elements()) != 0 {
		t.Errorf("a configured empty set must stay an empty set, got %v", m.Groups)
	}
	if m.Vars.IsNull() || len(m.Vars.Elements()) != 0 {
		t.Errorf("a configured empty map must stay an empty map, got %v", m.Vars)
	}
	if m.Address.IsNull() || m.Address.ValueString() != "" {
		t.Errorf("a configured empty address must stay \"\", got %v", m.Address)
	}
}

func TestReadIntoModelTakesTheServersValues(t *testing.T) {
	ctx := context.Background()
	item := &terrapod.InventoryItem{
		ID:          "invitem-1",
		WorkspaceID: "ws-9",
		Name:        "web-1",
		Address:     "10.0.0.4",
		Groups:      []string{"web", "eu_west"},
		Vars:        map[string]string{"ansible_user": "deploy"},
	}
	// Drift: the state holds nothing, the server holds a host in two groups.
	m := &inventoryItemModel{
		WorkspaceID: types.StringValue("ws-9"),
		Address:     types.StringNull(),
		Groups:      types.SetNull(types.StringType),
		Vars:        types.MapNull(types.StringType),
	}
	if d := readIntoModel(ctx, item, m); d.HasError() {
		t.Fatal(d)
	}
	if m.Address.ValueString() != "10.0.0.4" {
		t.Errorf("address = %v", m.Address)
	}
	if len(m.Groups.Elements()) != 2 || len(m.Vars.Elements()) != 1 {
		t.Errorf("groups = %v, vars = %v", m.Groups, m.Vars)
	}
}

// A configuration naming the bare uuid keeps that form, so neither spelling
// drifts into a perpetual diff on an attribute that forces replacement.
func TestReadIntoModelKeepsTheConfiguredWorkspaceForm(t *testing.T) {
	ctx := context.Background()
	item := &terrapod.InventoryItem{ID: "invitem-1", WorkspaceID: "ws-9999", Name: "web-1"}

	bare := &inventoryItemModel{WorkspaceID: types.StringValue("9999")}
	if d := readIntoModel(ctx, item, bare); d.HasError() {
		t.Fatal(d)
	}
	if bare.WorkspaceID.ValueString() != "9999" {
		t.Errorf("the bare configured id should be kept, got %q", bare.WorkspaceID.ValueString())
	}

	// An import starts with nothing, so the server's value is what makes the
	// imported item plan clean.
	imported := &inventoryItemModel{WorkspaceID: types.StringNull()}
	if d := readIntoModel(ctx, item, imported); d.HasError() {
		t.Fatal(d)
	}
	if imported.WorkspaceID.ValueString() != "ws-9999" {
		t.Errorf("an import must take the server's workspace, got %q", imported.WorkspaceID.ValueString())
	}

	other := &inventoryItemModel{WorkspaceID: types.StringValue("ws-1")}
	if d := readIntoModel(ctx, item, other); d.HasError() {
		t.Fatal(d)
	}
	if other.WorkspaceID.ValueString() != "ws-9999" {
		t.Errorf("a different workspace must take the server's value, got %q", other.WorkspaceID.ValueString())
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
		{"fqdn", "web-1.internal.example", false},
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
				t.Errorf("%q refused=%t, want %t: %v", tc.value, resp.Diagnostics.HasError(), tc.refused, resp.Diagnostics)
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

func TestGroupNamesValidatorRefusesDerivedAndNonIdentifierNames(t *testing.T) {
	ctx := context.Background()
	for _, tc := range []struct {
		name    string
		groups  []string
		refused bool
	}{
		{"identifiers", []string{"web", "eu_west", "_staging", "db2"}, false},
		{"all is derived", []string{"all"}, true},
		{"ungrouped is derived", []string{"ungrouped"}, true},
		{"hyphen", []string{"eu-west"}, true},
		{"leading digit", []string{"2web"}, true},
		{"dot", []string{"web.eu"}, true},
		{"empty", []string{""}, true},
		{"one bad among good", []string{"web", "all"}, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var resp validator.SetResponse
			groupNamesValidator{}.ValidateSet(ctx, validator.SetRequest{
				Path:        path.Root("groups"),
				ConfigValue: setOf(t, tc.groups...),
			}, &resp)
			if resp.Diagnostics.HasError() != tc.refused {
				t.Errorf("%v refused=%t, want %t: %v", tc.groups, resp.Diagnostics.HasError(), tc.refused, resp.Diagnostics)
			}
		})
	}
}

func TestGroupNamesValidatorIgnoresUnknownAndNull(t *testing.T) {
	ctx := context.Background()
	for _, v := range []types.Set{types.SetNull(types.StringType), types.SetUnknown(types.StringType)} {
		var resp validator.SetResponse
		groupNamesValidator{}.ValidateSet(ctx, validator.SetRequest{
			Path: path.Root("groups"), ConfigValue: v,
		}, &resp)
		if resp.Diagnostics.HasError() {
			t.Errorf("%v must not be validated: %v", v, resp.Diagnostics)
		}
	}
}

// Deliberately laxer than the group rule: ansible stores a variable whose name
// is not an identifier and only warns that `{{ name }}` cannot reach it, so
// refusing more would block working configurations.
func TestVarNamesValidatorRefusesOnlyAnEmptyKey(t *testing.T) {
	ctx := context.Background()
	for _, tc := range []struct {
		name    string
		vars    map[string]string
		refused bool
	}{
		{"identifier", map[string]string{"ansible_user": "deploy"}, false},
		{"hyphenated is fine", map[string]string{"odd-name": "x"}, false},
		{"empty key", map[string]string{"": "x"}, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var resp validator.MapResponse
			varNamesValidator{}.ValidateMap(ctx, validator.MapRequest{
				Path:        path.Root("vars"),
				ConfigValue: mapOf(t, tc.vars),
			}, &resp)
			if resp.Diagnostics.HasError() != tc.refused {
				t.Errorf("%v refused=%t, want %t: %v", tc.vars, resp.Diagnostics.HasError(), tc.refused, resp.Diagnostics)
			}
		})
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

func TestSameWorkspaceIsPrefixTolerant(t *testing.T) {
	if !sameWorkspace("ws-1", "1") || !sameWorkspace("1", "ws-1") || !sameWorkspace("ws-1", "ws-1") {
		t.Error("the prefixed and bare forms name the same workspace")
	}
	if sameWorkspace("ws-1", "ws-2") {
		t.Error("different workspaces must not compare equal")
	}
}
