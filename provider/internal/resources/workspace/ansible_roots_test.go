package workspace

import (
	"context"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/tfsdk"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-go/tftypes"
	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// A workspace's operations (#1966) and the ansible configure operation's roots
// and run defaults (#1929).
//
// This file used to be about keeping `config_directory`'s null and "" apart,
// because null meant "this workspace has no configure operation" and "" meant
// the repository root -- one field answering two questions. #1966 split them:
// `configure_enabled` answers the first, and `config_directory` is an ordinary
// non-nullable path whose empty value is the configuration version's root,
// exactly like `working_directory`. So the three-state tests are gone; their
// premise is what changed.
//
// What is tested instead is the hazard that REPLACED it, and it is the single
// defect this change could most easily ship: a Computed attribute whose plan
// value disagrees with what the server returns aborts the apply with "Provider
// produced inconsistent result after apply". For `config_directory` that would
// fire on EVERY workspace that does not set it (plan null, server ""); for
// `plan_apply_enabled` on every workspace that does not set it (plan null,
// server true). Hence roundTripIsConsistent below, which is the shape of test
// to reach for whenever a server supplies a default the provider does not.

// ── The round trip, which is the whole point ────────────────────────────────

// roundTripIsConsistent drives one apply the way Terraform does -- plan value
// -> create request -> server -> read-back -> final state -- and asserts
// Terraform's own consistency rule on the result: where the planned value was
// KNOWN the final state must equal it exactly, and only an UNKNOWN plan value
// may be filled in by the server.
//
// `server` stands in for the API: it receives what actually reached the wire
// and returns the Workspace the provider would read back, applying the same
// defaults the server does for anything the request omitted. That is what
// makes this a round-trip test rather than two assertions about a request.
func roundTripIsConsistent(
	t *testing.T,
	name string,
	plan func(*workspaceModel),
	server func(terrapod.CreateWorkspaceRequest) *terrapod.Workspace,
) *workspaceModel {
	t.Helper()
	ctx := context.Background()

	var m workspaceModel
	plan(&m)
	planned := m

	req, diags := buildCreateWorkspaceRequest(ctx, &m)
	if diags.HasError() {
		t.Fatalf("%s: create request: %v", name, diags)
	}

	ws := server(req)

	// The read starts from the plan, as Create does: the plan value is what a
	// null-preserving read consults.
	state := planned
	if diags := readWorkspaceIntoModel(ctx, ws, &state); diags.HasError() {
		t.Fatalf("%s: read: %v", name, diags)
	}

	// Terraform's rule, applied attribute by attribute for the three this
	// change touches.
	assertConsistent(t, name+" config_directory", planned.ConfigDirectory, state.ConfigDirectory)
	assertConsistentBool(t, name+" plan_apply_enabled", planned.PlanApplyEnabled, state.PlanApplyEnabled)
	assertConsistentBool(t, name+" configure_enabled", planned.ConfigureEnabled, state.ConfigureEnabled)
	return &state
}

func assertConsistent(t *testing.T, what string, planned, got types.String) {
	t.Helper()
	if planned.IsUnknown() {
		// The server may supply anything, but it must supply SOMETHING: a
		// Computed attribute left unknown in state is an error of its own.
		if got.IsUnknown() {
			t.Errorf("%s stayed unknown after apply — a Computed attribute must be resolved", what)
		}
		return
	}
	if !planned.Equal(got) {
		t.Errorf("%s: planned %v, got %v after apply — this is exactly "+
			"\"Provider produced inconsistent result after apply\"", what, planned, got)
	}
}

func assertConsistentBool(t *testing.T, what string, planned, got types.Bool) {
	t.Helper()
	if planned.IsUnknown() {
		if got.IsUnknown() {
			t.Errorf("%s stayed unknown after apply — a Computed attribute must be resolved", what)
		}
		return
	}
	if !planned.Equal(got) {
		t.Errorf("%s: planned %v, got %v after apply — this is exactly "+
			"\"Provider produced inconsistent result after apply\"", what, planned, got)
	}
}

// theServer applies the server's own defaults to whatever the request omitted,
// and echoes back whatever it carried. The defaults are the ones #1966 fixed:
// `config-directory` "", `plan-apply-enabled` TRUE, `configure-enabled` false.
func theServer(req terrapod.CreateWorkspaceRequest) *terrapod.Workspace {
	ws := &terrapod.Workspace{
		ID: "ws-a", Name: "a",
		ConfigDirectory:  req.ConfigDirectory, // "" when the request omitted it
		PlanApplyEnabled: true,
		ConfigureEnabled: false,
	}
	if req.PlanApplyEnabled != nil {
		ws.PlanApplyEnabled = *req.PlanApplyEnabled
	}
	if req.ConfigureEnabled != nil {
		ws.ConfigureEnabled = *req.ConfigureEnabled
	}
	return ws
}

func TestAnOmittedConfigDirectoryRoundTripsAgainstTheServersEmptyString(t *testing.T) {
	// THE test for this change. A practitioner who never mentions
	// `config_directory` plans it as unknown (Optional+Computed, no prior
	// state), the server stores and returns "", and state takes that "".
	//
	// Were the attribute Optional-only the plan value would be NULL, the
	// server would still answer "", and every apply of every workspace that
	// does not set the attribute would abort. That is not a hypothetical about
	// this schema: it is what the attribute looked like before #1966, when
	// null was a meaningful third state the server could return.
	state := roundTripIsConsistent(t, "omitted", func(m *workspaceModel) {
		m.ConfigDirectory = types.StringUnknown()
		m.PlanApplyEnabled = types.BoolUnknown()
		m.ConfigureEnabled = types.BoolUnknown()
	}, theServer)

	if state.ConfigDirectory.IsNull() || state.ConfigDirectory.IsUnknown() {
		t.Fatalf("config_directory resolved to %v, want a concrete value", state.ConfigDirectory)
	}
	if got := state.ConfigDirectory.ValueString(); got != "" {
		t.Errorf("config_directory = %q, want %q — the server's own default", got, "")
	}
	// The operations resolve to the server's defaults, not to Go's zero values.
	if !state.PlanApplyEnabled.ValueBool() {
		t.Error("plan_apply_enabled resolved false from an omitted attribute — the " +
			"server defaults it TRUE, and a workspace that cannot plan or apply is " +
			"not what a practitioner who said nothing asked for")
	}
	if state.ConfigureEnabled.ValueBool() {
		t.Error("configure_enabled resolved true from an omitted attribute — configure is opt-in")
	}
}

func TestEveryDeclaredShapeOfTheThreeRoundTripsConsistently(t *testing.T) {
	// The rest of the matrix. Each case is one apply, and the assertion is
	// the same consistency rule in every one of them.
	for _, tc := range []struct {
		name string
		plan func(*workspaceModel)
	}{
		{"the empty string is the configuration version's root", func(m *workspaceModel) {
			// The case an emptiness guard on the READ side would break: ""
			// mapped back to null disagrees with a plan of "".
			m.ConfigDirectory = types.StringValue("")
			m.PlanApplyEnabled = types.BoolUnknown()
			m.ConfigureEnabled = types.BoolUnknown()
		}},
		{"a subdirectory roots the operation there", func(m *workspaceModel) {
			m.ConfigDirectory = types.StringValue("ansible")
			m.PlanApplyEnabled = types.BoolUnknown()
			m.ConfigureEnabled = types.BoolUnknown()
		}},
		{"configure-only: plan/apply explicitly off", func(m *workspaceModel) {
			// The combination the two booleans exist for, and the one where a
			// plain bool on the request would silently lose the `false`.
			m.ConfigDirectory = types.StringValue("ansible")
			m.PlanApplyEnabled = types.BoolValue(false)
			m.ConfigureEnabled = types.BoolValue(true)
		}},
		{"both explicitly on", func(m *workspaceModel) {
			m.ConfigDirectory = types.StringValue("")
			m.PlanApplyEnabled = types.BoolValue(true)
			m.ConfigureEnabled = types.BoolValue(true)
		}},
		{"configure explicitly off against its own default", func(m *workspaceModel) {
			// `false` equals the server default here, which is precisely why
			// it is worth a case: an `omitempty`-shaped bug drops it and the
			// result happens to match, so this one passes either way. It is
			// here to document that, not to catch it -- the catching is done
			// by the configure-only case above, where the default differs.
			m.PlanApplyEnabled = types.BoolValue(true)
			m.ConfigureEnabled = types.BoolValue(false)
			m.ConfigDirectory = types.StringUnknown()
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			roundTripIsConsistent(t, tc.name, tc.plan, theServer)
		})
	}
}

// ── A false must survive, in both directions ───────────────────────────────

func TestAFalseOperationReachesTheWireAndIsNotReadAsAbsent(t *testing.T) {
	// The request structs hold these as *bool so that "I did not say" and
	// "turn it off" are different requests; the Workspace holds them as plain
	// bools with NO omitempty so a false survives the read. Both halves are
	// asserted here because either one alone lets a false vanish.
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
			m.PlanApplyEnabled = tc.val
			m.ConfigureEnabled = tc.val

			create, diags := buildCreateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("create: %v", diags)
			}
			checkOpBoolPtr(t, "create plan-apply-enabled", create.PlanApplyEnabled, tc.want)
			checkOpBoolPtr(t, "create configure-enabled", create.ConfigureEnabled, tc.want)

			update, diags := buildUpdateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("update: %v", diags)
			}
			checkOpBoolPtr(t, "update plan-apply-enabled", update.PlanApplyEnabled, tc.want)
			checkOpBoolPtr(t, "update configure-enabled", update.ConfigureEnabled, tc.want)
		})
	}

	// The read half: false must come back as a concrete false, never null.
	for _, want := range []bool{true, false} {
		var m workspaceModel
		ws := &terrapod.Workspace{
			ID: "ws-a", Name: "a",
			PlanApplyEnabled: want,
			ConfigureEnabled: want,
		}
		if diags := readWorkspaceIntoModel(context.Background(), ws, &m); diags.HasError() {
			t.Fatalf("read: %v", diags)
		}
		for _, f := range []struct {
			what string
			got  types.Bool
		}{
			{"plan_apply_enabled", m.PlanApplyEnabled},
			{"configure_enabled", m.ConfigureEnabled},
		} {
			if f.got.IsNull() || f.got.IsUnknown() {
				t.Fatalf("%s read back as %v, want a concrete value — these are "+
					"Optional+Computed, so a null read-back is the inconsistent-result shape",
					f.what, f.got)
			}
			if f.got.ValueBool() != want {
				t.Errorf("%s = %v, want %v", f.what, f.got.ValueBool(), want)
			}
		}
	}
}

func TestConfigDirectoryStillTravelsAsAPointerOnUpdateSoEmptyCanBeSent(t *testing.T) {
	// Create takes a plain string and the SDK drops an empty one, which is
	// harmless because "" is the server's default anyway. Update takes a
	// *string precisely so `&""` can travel: that is how an operator moves
	// the configure root back to the top of the repository, and a plain
	// string there would make it a silent no-op.
	ctx := context.Background()

	var m workspaceModel
	m.ConfigDirectory = types.StringValue("")

	update, diags := buildUpdateWorkspaceRequest(ctx, &m)
	if diags.HasError() {
		t.Fatalf("update: %v", diags)
	}
	checkStrPtr(t, "update config-directory", update.ConfigDirectory, strPtr(""))

	m.ConfigDirectory = types.StringValue("ansible")
	update, diags = buildUpdateWorkspaceRequest(ctx, &m)
	if diags.HasError() {
		t.Fatalf("update: %v", diags)
	}
	checkStrPtr(t, "update config-directory", update.ConfigDirectory, strPtr("ansible"))

	// Null and unknown both say nothing. There is no clear any more -- the
	// column is non-nullable and the server refuses a JSON null -- so neither
	// may invent one.
	for _, val := range []types.String{types.StringNull(), types.StringUnknown()} {
		m.ConfigDirectory = val
		update, diags = buildUpdateWorkspaceRequest(ctx, &m)
		if diags.HasError() {
			t.Fatalf("update: %v", diags)
		}
		checkStrPtr(t, "update config-directory from "+val.String(), update.ConfigDirectory, nil)
	}
}

// ── Schema shape ───────────────────────────────────────────────────────────

func TestTheThreeAttributesAreOptionalAndComputed(t *testing.T) {
	// The inversion of the test this file used to open with. `config_directory`
	// was Optional-and-NOT-Computed to keep a third state reachable; that
	// state is `configure_enabled` now, and Optional-only here would mean an
	// omitted attribute plans null against a server answer of "".
	var resp resource.SchemaResponse
	NewResource().Schema(context.Background(), resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema build error: %v", resp.Diagnostics)
	}

	for _, name := range []string{"config_directory", "plan_apply_enabled", "configure_enabled"} {
		a, ok := resp.Schema.Attributes[name]
		if !ok {
			t.Fatalf("%s is missing from the schema", name)
		}
		if !a.IsOptional() {
			t.Errorf("%s must be Optional — otherwise it cannot be declared in HCL at all", name)
		}
		if !a.IsComputed() {
			t.Errorf("%s must be Computed: the server supplies a default the provider "+
				"does not, so an Optional-only attribute plans null against a concrete "+
				"server answer and fails every apply that omits it with \"Provider "+
				"produced inconsistent result after apply\"", name)
		}
	}
}

// ── The both-false refusal, driven through ValidateConfig ───────────────────

func TestBothOperationsFalseIsRefusedAtPlanTime(t *testing.T) {
	// Driven through ValidateConfig itself, not through validateOperationsPair:
	// a test that hands the helper its arguments proves the helper formats a
	// diagnostic and says nothing about whether anything calls it. Deleting
	// the call from ValidateConfig must fail this.
	ctx := context.Background()

	for _, tc := range []struct {
		name      string
		planApply tftypes.Value
		configure tftypes.Value
		wantError bool
	}{
		{
			name:      "both explicitly false is refused",
			planApply: tftypes.NewValue(tftypes.Bool, false),
			configure: tftypes.NewValue(tftypes.Bool, false),
			wantError: true,
		},
		{
			name:      "configure-only is fine",
			planApply: tftypes.NewValue(tftypes.Bool, false),
			configure: tftypes.NewValue(tftypes.Bool, true),
		},
		{
			name:      "plan/apply-only is fine",
			planApply: tftypes.NewValue(tftypes.Bool, true),
			configure: tftypes.NewValue(tftypes.Bool, false),
		},
		{
			name: "plan/apply false with configure UNSET is left to the server",
			// Not refusable here: the server validates the MERGED row, and
			// this workspace may already hold configure_enabled = true. A
			// plan-time error would refuse a legitimate configuration.
			planApply: tftypes.NewValue(tftypes.Bool, false),
			configure: tftypes.NewValue(tftypes.Bool, nil),
		},
		{
			name:      "both unset is fine — that is the default workspace",
			planApply: tftypes.NewValue(tftypes.Bool, nil),
			configure: tftypes.NewValue(tftypes.Bool, nil),
		},
		{
			name: "an unknown is not a declaration",
			// Comes from an interpolation that has not resolved yet. Refusing
			// on it would fail a plan that is going to be perfectly valid.
			planApply: tftypes.NewValue(tftypes.Bool, tftypes.UnknownValue),
			configure: tftypes.NewValue(tftypes.Bool, false),
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := NewResource()
			cfg := configWith(t, ctx, r, map[string]tftypes.Value{
				"plan_apply_enabled": tc.planApply,
				"configure_enabled":  tc.configure,
			})

			var resp resource.ValidateConfigResponse
			r.(interface {
				ValidateConfig(context.Context, resource.ValidateConfigRequest, *resource.ValidateConfigResponse)
			}).ValidateConfig(ctx, resource.ValidateConfigRequest{Config: cfg}, &resp)

			if got := resp.Diagnostics.HasError(); got != tc.wantError {
				t.Fatalf("ValidateConfig error = %v, want %v (diags: %v)",
					got, tc.wantError, resp.Diagnostics)
			}
			if tc.wantError {
				// The message has to name the way out, or an operator who
				// wanted to park the workspace has nowhere to go.
				found := false
				for _, d := range resp.Diagnostics.Errors() {
					if strings.Contains(d.Detail(), "locked") {
						found = true
					}
				}
				if !found {
					t.Errorf("the refusal does not mention `locked`, which is the "+
						"reversible way to park a workspace: %v", resp.Diagnostics)
				}
			}
		})
	}
}

// configWith builds a resource config whose every attribute is null except the
// ones named, so a ValidateConfig test states only what it is about.
func configWith(t *testing.T, ctx context.Context, r resource.Resource, set map[string]tftypes.Value) tfsdk.Config {
	t.Helper()
	var sr resource.SchemaResponse
	r.Schema(ctx, resource.SchemaRequest{}, &sr)
	if sr.Diagnostics.HasError() {
		t.Fatalf("schema: %v", sr.Diagnostics)
	}

	objType, ok := sr.Schema.Type().TerraformType(ctx).(tftypes.Object)
	if !ok {
		t.Fatal("the workspace schema did not render as an object type")
	}
	vals := make(map[string]tftypes.Value, len(objType.AttributeTypes))
	for name, aType := range objType.AttributeTypes {
		if v, override := set[name]; override {
			vals[name] = v
			continue
		}
		vals[name] = tftypes.NewValue(aType, nil)
	}
	// Every name the caller asked for must exist, or the test would silently
	// assert nothing about a misspelled attribute.
	for name := range set {
		if _, ok := objType.AttributeTypes[name]; !ok {
			t.Fatalf("%s is not an attribute of the workspace schema", name)
		}
	}
	return tfsdk.Config{Schema: sr.Schema, Raw: tftypes.NewValue(objType, vals)}
}

// ── #1929's remaining five, unchanged by #1966 ─────────────────────────────

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
	// The unmanagedCollections contract: omitting the attribute leaves the
	// server's value alone. `config_directory` joins this set in #1966 --
	// previously it was the one exception, because a null meant "turn the
	// operation off" and had to travel as a clear. It no longer does.
	ctx := context.Background()

	var m workspaceModel
	m.InventoryPaths = types.ListNull(types.StringType)
	m.InventoryIgnorePaths = types.ListNull(types.StringType)
	m.DefaultPlaybook = types.StringNull()
	m.DefaultLimit = types.StringNull()
	m.InventoryIncludePlatform = types.BoolNull()
	m.ConfigDirectory = types.StringNull()
	m.PlanApplyEnabled = types.BoolNull()
	m.ConfigureEnabled = types.BoolNull()

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
	if update.ConfigDirectory != nil {
		t.Errorf("update sent config-directory %q when the config omitted it", *update.ConfigDirectory)
	}
	if update.PlanApplyEnabled != nil {
		t.Errorf("update sent plan-apply-enabled %v when the config omitted it", *update.PlanApplyEnabled)
	}
	if update.ConfigureEnabled != nil {
		t.Errorf("update sent configure-enabled %v when the config omitted it", *update.ConfigureEnabled)
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

// checkOpBoolPtr is checkBoolPtr's sibling for an attribute that is not
// allow-fork-pr-plans: that one's messages name their attribute, which would
// be actively misleading here.
func checkOpBoolPtr(t *testing.T, what string, got, want *bool) {
	t.Helper()
	switch {
	case want == nil && got != nil:
		t.Errorf("%s sent %v, want it omitted", what, *got)
	case want != nil && got == nil:
		t.Errorf("%s was dropped, want %v — an explicit value must reach the wire", what, *want)
	case want != nil && got != nil && *got != *want:
		t.Errorf("%s sent %v, want %v", what, *got, *want)
	}
}

func strPtr(s string) *string { return &s }
