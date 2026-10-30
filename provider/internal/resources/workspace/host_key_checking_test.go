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

// `ansible_host_key_checking` (#2318) on the workspace resource: whether a
// configure verifies the SSH host key of the hosts it targets.
//
// It is shaped exactly like the configure booleans beside it -- Optional +
// Computed, a `*bool` on both requests, an unconditional read-back -- and it
// still earns a test of its own, because it is the first of them whose server
// default is TRUE. That single difference moves where the defects are:
//
//   - the WRITE leg. On a false-defaulting sibling, a write leg that drops an
//     explicit `false` is INVISIBLE: the server's answer is `false` too, so the
//     round trip agrees with itself and the only symptom is a setting that
//     cannot be changed. Here the server answers `true`, the apply aborts with
//     "Provider produced inconsistent result after apply", and the practitioner
//     who asked to relax verification is the one who finds out. Same bug, loud
//     instead of silent -- which is why `explicitly false` below is the case
//     that matters most and why the request field is a pointer.
//   - the DIRECTION of a dropped write. An omitted attribute that reached the
//     wire as `false` would stand host-key verification DOWN on every workspace
//     an operator applied without mentioning it. A plain bool would do exactly
//     that, so "I did not say" and "stop verifying" being different requests is
//     a security property here, not a tidiness one.
//
// What is deliberately NOT asserted here: that relaxing it emits
// `ansible_ssh_common_args` on the `all` group, so an inventory host variable
// still wins. That is the server's inventory resolver, measured against real
// ansible where it lives (#2318). The provider's whole contract is to carry the
// boolean faithfully in both directions, and carrying it faithfully is what
// these tests pin.

// theHostKeyServer stands in for the API: it receives what actually reached the
// wire and returns the Workspace the provider would read back, applying the
// column's own default -- TRUE, which verifies -- to a request that omitted it.
func theHostKeyServer(req terrapod.CreateWorkspaceRequest) *terrapod.Workspace {
	ws := &terrapod.Workspace{ID: "ws-a", Name: "a", AnsibleHostKeyChecking: true}
	if req.AnsibleHostKeyChecking != nil {
		ws.AnsibleHostKeyChecking = *req.AnsibleHostKeyChecking
	}
	return ws
}

func TestHostKeyCheckingIsOptionalComputedWithUseStateForUnknown(t *testing.T) {
	var resp resource.SchemaResponse
	NewResource().Schema(context.Background(), resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema build error: %v", resp.Diagnostics)
	}

	const name = "ansible_host_key_checking"
	attr, ok := resp.Schema.Attributes[name]
	if !ok {
		t.Fatalf("attribute %q missing from the workspace schema", name)
	}
	if !attr.IsOptional() {
		t.Errorf("attribute %q must be Optional — it is the operator's switch", name)
	}
	if !attr.IsComputed() {
		t.Errorf("attribute %q must be Computed: the server projects a value for it even "+
			"when the config omits it, so Optional-only plans NULL against a server "+
			"answer of true and fails every apply that does not set it", name)
	}
	ba, ok := attr.(schema.BoolAttribute)
	if !ok {
		t.Fatalf("attribute %q is %T, want schema.BoolAttribute", name, attr)
	}
	// The modifier's IDENTITY, not merely that there is one: a different
	// modifier would satisfy a presence check while leaving the spurious-diff
	// behaviour in place. Its concrete type is unexported, so the expectation
	// comes from the library rather than from a string that could drift.
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

func TestHostKeyCheckingRoundTripsConsistently(t *testing.T) {
	ctx := context.Background()

	for _, tc := range []struct {
		name string
		val  types.Bool
		// The value state must hold afterwards. For an unknown plan that is the
		// server's default; for a known plan it is the planned value, and
		// anything else IS the inconsistent-result failure.
		want bool
	}{
		{"omitted takes the column's verifying default", types.BoolUnknown(), true},
		{"explicitly verifying", types.BoolValue(true), true},
		// The case this file exists for. A write leg that drops the false, or a
		// read-back that answers the server default regardless, fails here and
		// nowhere else -- and it is the configuration of the one practitioner
		// who needs to reach a host whose key they cannot pre-seed.
		{"explicitly relaxed", types.BoolValue(false), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceModel
			m.AnsibleHostKeyChecking = tc.val
			planned := m

			req, diags := buildCreateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("create request: %v", diags)
			}

			ws := theHostKeyServer(req)

			// The read starts from the plan, as Create does.
			state := planned
			if diags := readWorkspaceIntoModel(ctx, ws, &state); diags.HasError() {
				t.Fatalf("read: %v", diags)
			}

			assertConsistentBool(t, tc.name+" ansible_host_key_checking",
				planned.AnsibleHostKeyChecking, state.AnsibleHostKeyChecking)

			got := state.AnsibleHostKeyChecking
			if got.IsNull() || got.IsUnknown() {
				t.Fatalf("ansible_host_key_checking resolved to %v — a Computed attribute "+
					"over a non-nullable column must hold a concrete value after apply", got)
			}
			if got.ValueBool() != tc.want {
				t.Errorf("ansible_host_key_checking = %v, want %v", got.ValueBool(), tc.want)
			}
		})
	}
}

func TestHostKeyCheckingTravelsAsAPointerOnBothWritePaths(t *testing.T) {
	// Both request structs hold it as *bool so that "I did not say" and "stop
	// verifying" are different requests. With a default of TRUE that is a
	// security property: a plain bool would send `false` for an attribute the
	// configuration never mentions, so every apply would quietly stand host-key
	// verification down on a workspace nobody had asked to relax.
	ctx := context.Background()

	for _, tc := range []struct {
		name string
		val  types.Bool
		want *bool
	}{
		{"explicitly verifying", types.BoolValue(true), ptr(true)},
		{"explicitly relaxed", types.BoolValue(false), ptr(false)},
		{"omitted", types.BoolNull(), nil},
		{"unknown", types.BoolUnknown(), nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceModel
			m.AnsibleHostKeyChecking = tc.val

			create, diags := buildCreateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("create: %v", diags)
			}
			checkOpBoolPtr(t, "create ansible-host-key-checking",
				create.AnsibleHostKeyChecking, tc.want)

			update, diags := buildUpdateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("update: %v", diags)
			}
			checkOpBoolPtr(t, "update ansible-host-key-checking",
				update.AnsibleHostKeyChecking, tc.want)
		})
	}
}

func TestHostKeyCheckingReadsBackConcretelyInBothDirections(t *testing.T) {
	// The SDK holds it as a plain bool with no `omitempty` precisely so a false
	// survives the round trip rather than vanishing as absent, and the
	// attribute is Optional+Computed, so a null read-back is the
	// inconsistent-result shape. Both directions are asserted because a
	// read-back hardcoded to the server default would pass on `true` alone.
	ctx := context.Background()

	for _, verifying := range []bool{true, false} {
		t.Run(fmt.Sprintf("server says %v", verifying), func(t *testing.T) {
			var m workspaceModel
			ws := &terrapod.Workspace{
				ID: "ws-a", Name: "a",
				AnsibleHostKeyChecking: verifying,
			}
			if diags := readWorkspaceIntoModel(ctx, ws, &m); diags.HasError() {
				t.Fatalf("read: %v", diags)
			}
			got := m.AnsibleHostKeyChecking
			if got.IsNull() || got.IsUnknown() {
				t.Fatalf("ansible_host_key_checking read back as %v, want a concrete value", got)
			}
			if got.ValueBool() != verifying {
				t.Errorf("ansible_host_key_checking = %v, want %v", got.ValueBool(), verifying)
			}
		})
	}
}
