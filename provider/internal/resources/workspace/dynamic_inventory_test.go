package workspace

import (
	"context"
	"fmt"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/boolplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// The two dynamic-inventory switches (#2033) on the workspace resource:
// `dynamic_inventory_enabled` for ansible's `auto` plugin -- a declared plugin
// config -- and `inventory_scripts_enabled` for its `script` plugin, an
// executable. Both permit the source ON THE RUNNER; the API never executes one
// in either state.
//
// Two properties are worth a test each, and the second is the one a reading of
// the diff cannot establish.
//
// The first is the round trip. Both columns are non-nullable with a server-side
// default of FALSE, and the attributes are Optional+Computed, so a practitioner
// who never mentions either plans them UNKNOWN and the server fills them in.
// Were they Optional-only the plan value would be NULL against a server answer
// of `false`, and every apply of every workspace that did not set them would
// abort with "Provider produced inconsistent result after apply".
//
// The second is that the two are INDEPENDENT. They exist as a pair precisely so
// an operator can permit declared plugin configs without also permitting
// arbitrary executables, and the likeliest defect in a change that adds two
// adjacent booleans of the same type is a copy-paste that wires both model
// fields to one request field -- or reads one column into both attributes. Every
// case where the two carry the SAME value passes under that bug. So the cases
// that catch it are the ones where they differ, and they are in all three of the
// request, the read and the data source's own test next door.

// theDynamicInventoryServer stands in for the API: it receives what actually
// reached the wire and returns the Workspace the provider would read back,
// applying the server's own default -- FALSE, which refuses -- to anything the
// request omitted.
func theDynamicInventoryServer(req terrapod.CreateWorkspaceRequest) *terrapod.Workspace {
	ws := &terrapod.Workspace{ID: "ws-a", Name: "a"}
	if req.DynamicInventoryEnabled != nil {
		ws.DynamicInventoryEnabled = *req.DynamicInventoryEnabled
	}
	if req.InventoryScriptsEnabled != nil {
		ws.InventoryScriptsEnabled = *req.InventoryScriptsEnabled
	}
	return ws
}

func TestTheDynamicInventorySwitchesAreOptionalComputedWithUseStateForUnknown(t *testing.T) {
	var resp resource.SchemaResponse
	NewResource().Schema(context.Background(), resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema build error: %v", resp.Diagnostics)
	}

	for _, name := range []string{"dynamic_inventory_enabled", "inventory_scripts_enabled"} {
		attr, ok := resp.Schema.Attributes[name]
		if !ok {
			t.Fatalf("attribute %q missing from the workspace schema", name)
		}
		if !attr.IsOptional() {
			t.Errorf("attribute %q must be Optional — it is the operator's switch", name)
		}
		if !attr.IsComputed() {
			t.Errorf("attribute %q must be Computed: the server projects a value for it "+
				"even when the config omits it, so Optional-only plans NULL against a "+
				"server answer of false and fails every apply that does not set it", name)
		}
		ba, ok := attr.(schema.BoolAttribute)
		if !ok {
			t.Fatalf("attribute %q is %T, want schema.BoolAttribute — two booleans "+
				"rather than one parsed value is the whole point of the pair", name, attr)
		}
		// Assert the modifier's IDENTITY, not merely that there is one: a
		// different modifier would satisfy a presence check while leaving the
		// spurious-diff behaviour in place. Its concrete type is unexported, so
		// the expectation is taken from the library itself rather than written
		// out as a string that could drift.
		want := fmt.Sprintf("%T", boolplanmodifier.UseStateForUnknown())
		found := false
		for _, pm := range ba.PlanModifiers {
			if fmt.Sprintf("%T", pm) == want {
				found = true
			}
		}
		if !found {
			got := make([]string, 0, len(ba.PlanModifiers))
			for _, pm := range ba.PlanModifiers {
				got = append(got, fmt.Sprintf("%T", pm))
			}
			t.Errorf("attribute %q has plan modifiers %v, want one of type %s like every "+
				"neighbour in the configure block — without it an Optional+Computed "+
				"attribute replans as unknown and shows a spurious diff on every plan",
				name, got, want)
		}
	}
}

func TestTheDynamicInventorySwitchesRoundTripConsistently(t *testing.T) {
	ctx := context.Background()

	for _, tc := range []struct {
		name     string
		dyn, scr types.Bool
		// The value state must hold afterwards. For an unknown plan that is
		// the server's default; for a known plan it is the planned value, and
		// anything else IS the inconsistent-result failure.
		wantDyn, wantScr bool
	}{
		{"both omitted take the server's refusing default",
			types.BoolUnknown(), types.BoolUnknown(), false, false},
		{"both permitted", types.BoolValue(true), types.BoolValue(true), true, true},
		{"both explicitly refused", types.BoolValue(false), types.BoolValue(false), false, false},
		// The two that catch an aliased wiring.
		{"declared configs permitted, executables refused",
			types.BoolValue(true), types.BoolValue(false), true, false},
		{"executables permitted, declared configs refused",
			types.BoolValue(false), types.BoolValue(true), false, true},
		// One declared, one left to the server: the mixed plan a practitioner
		// who only cares about the stricter half actually writes.
		{"scripts refused explicitly, the plugin switch left to the server",
			types.BoolUnknown(), types.BoolValue(false), false, false},
		{"plugins permitted, scripts left to the server",
			types.BoolValue(true), types.BoolUnknown(), true, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceModel
			m.DynamicInventoryEnabled = tc.dyn
			m.InventoryScriptsEnabled = tc.scr
			planned := m

			req, diags := buildCreateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("create request: %v", diags)
			}

			ws := theDynamicInventoryServer(req)

			// The read starts from the plan, as Create does.
			state := planned
			if diags := readWorkspaceIntoModel(ctx, ws, &state); diags.HasError() {
				t.Fatalf("read: %v", diags)
			}

			assertConsistentBool(t, tc.name+" dynamic_inventory_enabled",
				planned.DynamicInventoryEnabled, state.DynamicInventoryEnabled)
			assertConsistentBool(t, tc.name+" inventory_scripts_enabled",
				planned.InventoryScriptsEnabled, state.InventoryScriptsEnabled)

			for _, f := range []struct {
				what string
				got  types.Bool
				want bool
			}{
				{"dynamic_inventory_enabled", state.DynamicInventoryEnabled, tc.wantDyn},
				{"inventory_scripts_enabled", state.InventoryScriptsEnabled, tc.wantScr},
			} {
				if f.got.IsNull() || f.got.IsUnknown() {
					t.Fatalf("%s resolved to %v — a Computed attribute over a "+
						"non-nullable column must hold a concrete value after apply",
						f.what, f.got)
				}
				if f.got.ValueBool() != f.want {
					t.Errorf("%s = %v, want %v", f.what, f.got.ValueBool(), f.want)
				}
			}
		})
	}
}

func TestADynamicInventorySwitchTravelsAsAPointerOnBothWritePaths(t *testing.T) {
	// The request structs hold both as *bool so that "I did not say" and
	// "refuse these sources" are different requests. That matters most on
	// UPDATE: a plain bool there would send `false` for an attribute the
	// configuration never mentions, so every apply would quietly re-refuse the
	// plugin sources on a workspace an operator had deliberately permitted.
	ctx := context.Background()

	for _, tc := range []struct {
		name string
		val  types.Bool
		want *bool
	}{
		{"explicitly permitted", types.BoolValue(true), ptr(true)},
		{"explicitly refused", types.BoolValue(false), ptr(false)},
		{"omitted", types.BoolNull(), nil},
		{"unknown", types.BoolUnknown(), nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceModel
			m.DynamicInventoryEnabled = tc.val
			m.InventoryScriptsEnabled = tc.val

			create, diags := buildCreateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("create: %v", diags)
			}
			checkOpBoolPtr(t, "create dynamic-inventory-enabled",
				create.DynamicInventoryEnabled, tc.want)
			checkOpBoolPtr(t, "create inventory-scripts-enabled",
				create.InventoryScriptsEnabled, tc.want)

			update, diags := buildUpdateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("update: %v", diags)
			}
			checkOpBoolPtr(t, "update dynamic-inventory-enabled",
				update.DynamicInventoryEnabled, tc.want)
			checkOpBoolPtr(t, "update inventory-scripts-enabled",
				update.InventoryScriptsEnabled, tc.want)
		})
	}

	// And the pair stays independent on the wire. Every case above sets both
	// attributes to the same value, so all of them pass if one request field is
	// assigned twice; these two do not.
	for _, tc := range []struct {
		name     string
		dyn, scr bool
	}{
		{"declared configs permitted, executables refused", true, false},
		{"executables permitted, declared configs refused", false, true},
	} {
		t.Run("independent on the wire: "+tc.name, func(t *testing.T) {
			var m workspaceModel
			m.DynamicInventoryEnabled = types.BoolValue(tc.dyn)
			m.InventoryScriptsEnabled = types.BoolValue(tc.scr)

			create, diags := buildCreateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("create: %v", diags)
			}
			checkOpBoolPtr(t, "create dynamic-inventory-enabled",
				create.DynamicInventoryEnabled, ptr(tc.dyn))
			checkOpBoolPtr(t, "create inventory-scripts-enabled",
				create.InventoryScriptsEnabled, ptr(tc.scr))

			update, diags := buildUpdateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("update: %v", diags)
			}
			checkOpBoolPtr(t, "update dynamic-inventory-enabled",
				update.DynamicInventoryEnabled, ptr(tc.dyn))
			checkOpBoolPtr(t, "update inventory-scripts-enabled",
				update.InventoryScriptsEnabled, ptr(tc.scr))
		})
	}
}

func TestTheDynamicInventorySwitchesReadBackConcretelyAndIndependently(t *testing.T) {
	// The SDK holds both as plain bools with no `omitempty` precisely so a
	// false survives the round trip rather than vanishing as absent, and these
	// attributes are Optional+Computed, so a null read-back is the
	// inconsistent-result shape. The differing pairs are what catch one column
	// being read into both attributes.
	ctx := context.Background()

	for _, tc := range []struct {
		name     string
		dyn, scr bool
	}{
		{"both refused", false, false},
		{"both permitted", true, true},
		{"declared configs permitted, executables refused", true, false},
		{"executables permitted, declared configs refused", false, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceModel
			ws := &terrapod.Workspace{
				ID: "ws-a", Name: "a",
				DynamicInventoryEnabled: tc.dyn,
				InventoryScriptsEnabled: tc.scr,
			}
			if diags := readWorkspaceIntoModel(ctx, ws, &m); diags.HasError() {
				t.Fatalf("read: %v", diags)
			}
			for _, f := range []struct {
				what string
				got  types.Bool
				want bool
			}{
				{"dynamic_inventory_enabled", m.DynamicInventoryEnabled, tc.dyn},
				{"inventory_scripts_enabled", m.InventoryScriptsEnabled, tc.scr},
			} {
				if f.got.IsNull() || f.got.IsUnknown() {
					t.Fatalf("%s read back as %v, want a concrete value", f.what, f.got)
				}
				if f.got.ValueBool() != f.want {
					t.Errorf("%s = %v, want %v", f.what, f.got.ValueBool(), f.want)
				}
			}
		})
	}
}
