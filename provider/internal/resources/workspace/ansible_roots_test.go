package workspace

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"
	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// The ansible configure operation's roots and run defaults (#1929). They were
// a separate terrapod_inventory_settings resource; they are workspace columns
// now, so they are attributes here.
//
// `config_directory` is the one with a trap in it and most of this file is
// about it: null and "" are DIFFERENT values, and Terraform distinguishes them
// natively, so the provider only has to avoid collapsing them. The failure if
// it does is not a lost value but a wrong one -- every workspace rooted at its
// repository root would read back as having no configure operation at all, and
// every apply that writes `config_directory = ""` would abort with "Provider
// produced inconsistent result after apply".

func TestConfigDirectoryIsOptionalAndNotComputed(t *testing.T) {
	// Not a style preference. Every neighbour is Optional+Computed with
	// UseStateForUnknown so an unmanaged server value survives, and that
	// pattern CANNOT express this attribute: a config that omitted it would
	// plan as the prior state, so once a configure operation had been set up
	// there would be no way to turn it off from HCL. Optional-only makes a
	// removal plan as null, which Update sends as an explicit clear.
	var resp resource.SchemaResponse
	NewResource().Schema(context.Background(), resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema build error: %v", resp.Diagnostics)
	}

	a, ok := resp.Schema.Attributes["config_directory"]
	if !ok {
		t.Fatal("config_directory is missing from the schema")
	}
	if !a.IsOptional() {
		t.Error("config_directory must be Optional — otherwise a configure operation " +
			"cannot be declared in HCL at all")
	}
	if a.IsComputed() {
		t.Error("config_directory must NOT be Computed: with UseStateForUnknown a config " +
			"that drops the attribute plans as the prior state, so the configure " +
			"operation could never be turned off from HCL")
	}
}

func TestConfigDirectoryNullEmptyAndValueAreThreeDifferentRequests(t *testing.T) {
	ctx := context.Background()

	cases := []struct {
		name string
		val  types.String
		// create: want a pointer (and its value), or nil for omitted.
		wantCreate *string
		// update: want a value, a clear, or neither.
		wantUpdate *string
		wantClear  bool
	}{
		{
			name: "a subdirectory roots the operation there",
			val:  types.StringValue("ansible"),
			// A plain value on both.
			wantCreate: strPtr("ansible"), wantUpdate: strPtr("ansible"),
		},
		{
			name: "the empty string is the repository root and must travel",
			val:  types.StringValue(""),
			// The case an emptiness guard would silently drop. If create
			// omits it the workspace is created with no configure operation
			// at all, and the apply then fails on the read-back.
			wantCreate: strPtr(""), wantUpdate: strPtr(""),
		},
		{
			name: "null means no configure operation: omitted on create, cleared on update",
			val:  types.StringNull(),
			// Omitted on create because null IS the server-side default.
			// Cleared on update because the attribute is Optional-only, so a
			// null plan value means the operator removed it from the config.
			wantCreate: nil, wantUpdate: nil, wantClear: true,
		},
		{
			name: "unknown is left alone on both",
			val:  types.StringUnknown(),
			// An unknown is not an instruction. Clearing on unknown would
			// turn the configure operation off mid-plan.
			wantCreate: nil, wantUpdate: nil, wantClear: false,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceModel
			m.ConfigDirectory = tc.val

			create, diags := buildCreateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("create: %v", diags)
			}
			checkStrPtr(t, "create config-directory", create.ConfigDirectory, tc.wantCreate)

			update, diags := buildUpdateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("update: %v", diags)
			}
			checkStrPtr(t, "update config-directory", update.ConfigDirectory, tc.wantUpdate)
			if update.ClearConfigDirectory != tc.wantClear {
				t.Errorf("update ClearConfigDirectory = %v, want %v", update.ClearConfigDirectory, tc.wantClear)
			}
		})
	}
}

func TestReadKeepsConfigDirectoryNullAndEmptyApart(t *testing.T) {
	// The read half of the same property. `if *ws.ConfigDirectory != ""` here
	// is the mistake: it reads the repository-root case back as null, so the
	// apply that set it fails with "inconsistent result after apply".
	ctx := context.Background()

	cases := []struct {
		name      string
		from      *string
		wantNull  bool
		wantValue string
	}{
		{"nil is no configure operation", nil, true, ""},
		{"empty is the repository root", strPtr(""), false, ""},
		{"a value is that subdirectory", strPtr("ansible"), false, "ansible"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceModel
			ws := &terrapod.Workspace{ID: "ws-a", Name: "a", ConfigDirectory: tc.from}
			if diags := readWorkspaceIntoModel(ctx, ws, &m); diags.HasError() {
				t.Fatalf("read: %v", diags)
			}
			if m.ConfigDirectory.IsNull() != tc.wantNull {
				t.Fatalf("config_directory null = %v, want %v (value %q)",
					m.ConfigDirectory.IsNull(), tc.wantNull, m.ConfigDirectory.ValueString())
			}
			if !tc.wantNull && m.ConfigDirectory.ValueString() != tc.wantValue {
				t.Errorf("config_directory = %q, want %q", m.ConfigDirectory.ValueString(), tc.wantValue)
			}
		})
	}
}

// ── The other five ──────────────────────────────────────────────────────────

func TestIncludePlatformReachesTheWireIncludingFalse(t *testing.T) {
	// The server default is TRUE, so false is the interesting direction: a
	// plain bool on the request would drop it and "resolve the git source
	// alone" would be a silent no-op.
	ctx := context.Background()

	for _, tc := range []struct {
		name string
		val  types.Bool
		want *bool
	}{
		{"explicitly true", types.BoolValue(true), ptr(true)},
		{"explicitly false", types.BoolValue(false), ptr(false)},
		{"omitted", types.BoolNull(), nil},
		{"unknown", types.BoolUnknown(), nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceModel
			m.InventoryIncludePlatform = tc.val

			create, diags := buildCreateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("create: %v", diags)
			}
			checkBoolPtr(t, "create", create.InventoryIncludePlatform, tc.want)

			update, diags := buildUpdateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("update: %v", diags)
			}
			checkBoolPtr(t, "update", update.InventoryIncludePlatform, tc.want)
		})
	}
}

func TestIncludePlatformReadsBackAConcreteValue(t *testing.T) {
	// Optional+Computed, so a null read-back is the "inconsistent result after
	// apply" shape. Both values must survive, and false especially: the SDK
	// resolves an ABSENT attribute to true, so a decoder bug here shows up as
	// a workspace reporting its declared rows as contributing when the
	// operator has switched them off.
	ctx := context.Background()

	for _, want := range []bool{true, false} {
		var m workspaceModel
		ws := &terrapod.Workspace{ID: "ws-a", Name: "a", InventoryIncludePlatform: want}
		if diags := readWorkspaceIntoModel(ctx, ws, &m); diags.HasError() {
			t.Fatalf("read: %v", diags)
		}
		if m.InventoryIncludePlatform.IsNull() || m.InventoryIncludePlatform.IsUnknown() {
			t.Fatalf("inventory_include_platform read back as %v, want a concrete value",
				m.InventoryIncludePlatform)
		}
		if got := m.InventoryIncludePlatform.ValueBool(); got != want {
			t.Errorf("inventory_include_platform = %v, want %v", got, want)
		}
	}
}

func TestPlaybookAndLimitTravelAsPointersSoTheyCanBeCleared(t *testing.T) {
	ctx := context.Background()

	var m workspaceModel
	m.DefaultPlaybook = types.StringValue("")
	m.DefaultLimit = types.StringValue("")

	// Create omits an empty string: it is the server-side default, so there is
	// nothing to say.
	create, diags := buildCreateWorkspaceRequest(ctx, &m)
	if diags.HasError() {
		t.Fatalf("create: %v", diags)
	}
	if create.DefaultPlaybook != "" || create.DefaultLimit != "" {
		t.Errorf("create sent playbook=%q limit=%q, want both empty",
			create.DefaultPlaybook, create.DefaultLimit)
	}

	// Update must send them, because "" is how an operator stops defaulting
	// to a playbook and widens a run back to the whole inventory.
	update, diags := buildUpdateWorkspaceRequest(ctx, &m)
	if diags.HasError() {
		t.Fatalf("update: %v", diags)
	}
	checkStrPtr(t, "update default-playbook", update.DefaultPlaybook, strPtr(""))
	checkStrPtr(t, "update default-limit", update.DefaultLimit, strPtr(""))
}

func TestInventoryPathsPreserveOrderAndAnEmptyListClears(t *testing.T) {
	// Order is precedence for ansible, so a reordering is a behaviour change
	// and the list must not be treated as a set anywhere along the path.
	ctx := context.Background()

	var m workspaceModel
	m.InventoryPaths = types.ListValueMust(types.StringType, []attr.Value{
		types.StringValue("inventory/production"),
		types.StringValue("inventory/extra.yml"),
	})
	m.InventoryIgnorePaths = types.ListValueMust(types.StringType, []attr.Value{})

	create, diags := buildCreateWorkspaceRequest(ctx, &m)
	if diags.HasError() {
		t.Fatalf("create: %v", diags)
	}
	if len(create.InventoryPaths) != 2 ||
		create.InventoryPaths[0] != "inventory/production" ||
		create.InventoryPaths[1] != "inventory/extra.yml" {
		t.Errorf("create sent inventory-paths %v — order is precedence and must survive",
			create.InventoryPaths)
	}
	// An explicit empty list is a real request: it removes every entry. A nil
	// slice would be omitted by the SDK and marshal as absent, so the clear
	// has to arrive as a non-nil empty slice.
	if create.InventoryIgnorePaths == nil {
		t.Error("create dropped an explicit empty inventory_ignore_paths — the one " +
			"config that clears it would silently do nothing")
	} else if len(create.InventoryIgnorePaths) != 0 {
		t.Errorf("create sent inventory-ignore-paths %v, want empty", create.InventoryIgnorePaths)
	}

	update, diags := buildUpdateWorkspaceRequest(ctx, &m)
	if diags.HasError() {
		t.Fatalf("update: %v", diags)
	}
	if len(update.InventoryPaths) != 2 || update.InventoryPaths[0] != "inventory/production" {
		t.Errorf("update sent inventory-paths %v", update.InventoryPaths)
	}
	if update.InventoryIgnorePaths == nil {
		t.Error("update dropped an explicit empty inventory_ignore_paths")
	}
}

func TestOmittedRootsAreNotSentSoAnUnmanagedValueSurvives(t *testing.T) {
	// The five Optional+Computed ones follow the unmanagedCollections
	// contract: omitting the attribute leaves the server's value alone. Only
	// config_directory differs, and its own test above covers that.
	ctx := context.Background()

	var m workspaceModel
	m.InventoryPaths = types.ListNull(types.StringType)
	m.InventoryIgnorePaths = types.ListNull(types.StringType)
	m.DefaultPlaybook = types.StringNull()
	m.DefaultLimit = types.StringNull()
	m.InventoryIncludePlatform = types.BoolNull()

	update, diags := buildUpdateWorkspaceRequest(ctx, &m)
	if diags.HasError() {
		t.Fatalf("update: %v", diags)
	}
	if update.InventoryPaths != nil {
		t.Errorf("update sent inventory-paths %v when the config omitted it", update.InventoryPaths)
	}
	if update.InventoryIgnorePaths != nil {
		t.Errorf("update sent inventory-ignore-paths %v when the config omitted it",
			update.InventoryIgnorePaths)
	}
	if update.DefaultPlaybook != nil {
		t.Errorf("update sent default-playbook %q when the config omitted it", *update.DefaultPlaybook)
	}
	if update.DefaultLimit != nil {
		t.Errorf("update sent default-limit %q when the config omitted it", *update.DefaultLimit)
	}
	if update.InventoryIncludePlatform != nil {
		t.Errorf("update sent inventory-include-platform %v when the config omitted it",
			*update.InventoryIncludePlatform)
	}
}

func TestReadPreservesNullListsWhenTheServerHasNone(t *testing.T) {
	// The null-versus-empty-list rule the four existing lists follow: a prior
	// null plus a server empty stays null, so a config that never declared the
	// attribute does not plan a diff against `[]`.
	ctx := context.Background()

	var m workspaceModel
	m.InventoryPaths = types.ListNull(types.StringType)
	m.InventoryIgnorePaths = types.ListNull(types.StringType)
	ws := &terrapod.Workspace{ID: "ws-a", Name: "a"}
	if diags := readWorkspaceIntoModel(ctx, ws, &m); diags.HasError() {
		t.Fatalf("read: %v", diags)
	}
	if !m.InventoryPaths.IsNull() {
		t.Errorf("inventory_paths materialised as %v from a prior null and an empty server value",
			m.InventoryPaths)
	}
	if !m.InventoryIgnorePaths.IsNull() {
		t.Errorf("inventory_ignore_paths materialised as %v", m.InventoryIgnorePaths)
	}

	// With values present it reads them, in order.
	var m2 workspaceModel
	m2.InventoryPaths = types.ListNull(types.StringType)
	ws2 := &terrapod.Workspace{
		ID: "ws-a", Name: "a",
		InventoryPaths: []string{"inventory/production", "inventory/extra.yml"},
	}
	if diags := readWorkspaceIntoModel(ctx, ws2, &m2); diags.HasError() {
		t.Fatalf("read: %v", diags)
	}
	els := m2.InventoryPaths.Elements()
	if len(els) != 2 || els[0].(types.String).ValueString() != "inventory/production" {
		t.Errorf("inventory_paths read back as %v — order is precedence", els)
	}
}

func checkStrPtr(t *testing.T, what string, got, want *string) {
	t.Helper()
	switch {
	case want == nil && got != nil:
		t.Errorf("%s sent %q, want it omitted", what, *got)
	case want != nil && got == nil:
		t.Errorf("%s was dropped, want %q", what, *want)
	case want != nil && got != nil && *got != *want:
		t.Errorf("%s sent %q, want %q", what, *got, *want)
	}
}

func strPtr(s string) *string { return &s }
