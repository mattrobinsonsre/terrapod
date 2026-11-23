package workspace

import (
	"context"
	"fmt"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/validators"
)

// `ssh_host_key_policy` (#2592) on the workspace resource: how a configure
// verifies the SSH host keys of the hosts it targets.
//
// It REPLACED `ansible_host_key_checking` (#2318), a `*bool` on both requests,
// and the change of type is what moves the defects. The pointer existed so that
// "I did not say" and "stop verifying" were different requests, because a plain
// bool would have sent `false` for an attribute the configuration never
// mentioned. A string needs no pointer for that: the SDK omits an empty field,
// and the server REFUSES "" outright, so there is no third state to lose. What
// a string gains instead is a value set, and a value set brings its own failures:
//
//   - a value OpenSSH does not define. The server answers 422, which arrives
//     mid-APPLY -- on a workspace that is one resource among many, after other
//     resources have already changed. Hence the plan-time validator, and hence a
//     test that the schema actually carries it: a validator written and not
//     wired is the recorded flag-nothing-reads shape.
//   - a BOOLEAN carried forward. A configuration migrating from #2318 says
//     `= true`, which Terraform coerces to the string "true" before the provider
//     sees it. Without a clause for that, the diagnostic lists three strings and
//     leaves the practitioner to guess which of them their boolean had meant.
//   - the MIDDLE state. `accept-new` is the whole reason the boolean was
//     replaced, so a fixture driving only strict/off would pass whether or not
//     the provider could carry it at all.
//
// What is deliberately NOT asserted here: that the policy rides the run's own
// ssh anchor and nothing in the inventory can override it. That is the server's
// renderer, measured against real OpenSSH argv where it lives (#2592). The
// provider's contract is to carry the string faithfully in both directions, and
// carrying it faithfully is what these tests pin.

// thePolicyServer stands in for the API: it receives what actually reached the
// wire and returns the Workspace the provider would read back, applying the
// column's own default -- "strict" -- to a request that omitted it.
func thePolicyServer(req terrapod.CreateWorkspaceRequest) *terrapod.Workspace {
	ws := &terrapod.Workspace{ID: "ws-a", Name: "a", SSHHostKeyPolicy: "strict"}
	if req.SSHHostKeyPolicy != "" {
		ws.SSHHostKeyPolicy = req.SSHHostKeyPolicy
	}
	return ws
}

func TestSSHHostKeyPolicyIsOptionalComputedWithUseStateForUnknown(t *testing.T) {
	var resp resource.SchemaResponse
	NewResource().Schema(context.Background(), resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema build error: %v", resp.Diagnostics)
	}

	const name = "ssh_host_key_policy"
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
			"answer of \"strict\" and fails every apply that does not set it", name)
	}
	sa, ok := attr.(schema.StringAttribute)
	if !ok {
		t.Fatalf("attribute %q is %T, want schema.StringAttribute — a tri-state is not a "+
			"boolean, which is the whole of #2592", name, attr)
	}
	// The modifier's IDENTITY, not merely that there is one: a different
	// modifier would satisfy a presence check while leaving the spurious-diff
	// behaviour in place. Its concrete type is unexported, so the expectation
	// comes from the library rather than from a string that could drift.
	want := fmt.Sprintf("%T", stringplanmodifier.UseStateForUnknown())
	found := false
	for _, pm := range sa.PlanModifiers {
		if fmt.Sprintf("%T", pm) == want {
			found = true
		}
	}
	if !found {
		got := make([]string, 0, len(sa.PlanModifiers))
		for _, pm := range sa.PlanModifiers {
			got = append(got, fmt.Sprintf("%T", pm))
		}
		t.Errorf("attribute %q has plan modifiers %v, want one of type %s like every "+
			"neighbour in the configure block — without it an Optional+Computed "+
			"attribute replans as unknown and shows a spurious diff on every plan",
			name, got, want)
	}

	// The validator must be WIRED, not merely written. Its own behaviour is
	// tested in internal/validators; what this asserts is that the schema
	// reaches it, which is the half a validator's own unit tests cannot see.
	wantV := fmt.Sprintf("%T", validators.SSHHostKeyPolicy{})
	for _, v := range sa.Validators {
		if fmt.Sprintf("%T", v) == wantV {
			return
		}
	}
	t.Errorf("attribute %q carries no %s, so a value OpenSSH does not define is refused "+
		"by the server mid-apply instead of at plan time", name, wantV)
}

func TestSSHHostKeyPolicyRoundTripsConsistently(t *testing.T) {
	ctx := context.Background()

	for _, tc := range []struct {
		name string
		val  types.String
		// The value state must hold afterwards. For an unknown plan that is the
		// server's default; for a known plan it is the planned value, and
		// anything else IS the inconsistent-result failure.
		want string
	}{
		{"omitted takes the column's strict default", types.StringUnknown(), "strict"},
		{"explicitly strict", types.StringValue("strict"), "strict"},
		// The case this file exists for. `accept-new` is the state the boolean
		// could not hold, so a write leg that dropped the value, or a read-back
		// that answered the server default regardless, fails here and nowhere
		// else -- and it is the configuration of the one operator with approved
		// keys on some hosts and not others.
		{"accept-new, the state the boolean could not hold", types.StringValue("accept-new"), "accept-new"},
		{"explicitly off", types.StringValue("off"), "off"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceModel
			m.SSHHostKeyPolicy = tc.val
			planned := m

			req, diags := buildCreateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("create request: %v", diags)
			}

			ws := thePolicyServer(req)

			// The read starts from the plan, as Create does.
			state := planned
			if diags := readWorkspaceIntoModel(ctx, ws, &state); diags.HasError() {
				t.Fatalf("read: %v", diags)
			}

			assertConsistent(t, tc.name+" ssh_host_key_policy",
				planned.SSHHostKeyPolicy, state.SSHHostKeyPolicy)

			got := state.SSHHostKeyPolicy
			if got.IsNull() || got.IsUnknown() {
				t.Fatalf("ssh_host_key_policy resolved to %v — a Computed attribute "+
					"over a non-nullable column must hold a concrete value after apply", got)
			}
			if got.ValueString() != tc.want {
				t.Errorf("ssh_host_key_policy = %q, want %q", got.ValueString(), tc.want)
			}
		})
	}
}

func TestSSHHostKeyPolicyTravelsOnBothWritePaths(t *testing.T) {
	// Both request structs hold it as a plain string, which the SDK omits when
	// empty. That is what makes "I did not say" and "set a policy" different
	// requests without a pointer -- and the empty case is the one that matters,
	// because the server refuses "" rather than reading it as "take the
	// default". A write leg that sent it unconditionally would 422 every apply
	// that did not mention the attribute.
	ctx := context.Background()

	for _, tc := range []struct {
		name string
		val  types.String
		want string
	}{
		{"explicitly strict", types.StringValue("strict"), "strict"},
		{"accept-new", types.StringValue("accept-new"), "accept-new"},
		{"explicitly off", types.StringValue("off"), "off"},
		{"omitted", types.StringNull(), ""},
		{"unknown", types.StringUnknown(), ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceModel
			m.SSHHostKeyPolicy = tc.val

			create, diags := buildCreateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("create: %v", diags)
			}
			if create.SSHHostKeyPolicy != tc.want {
				t.Errorf("create ssh-host-key-policy = %q, want %q",
					create.SSHHostKeyPolicy, tc.want)
			}

			update, diags := buildUpdateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("update: %v", diags)
			}
			if update.SSHHostKeyPolicy != tc.want {
				t.Errorf("update ssh-host-key-policy = %q, want %q",
					update.SSHHostKeyPolicy, tc.want)
			}
		})
	}
}

func TestSSHHostKeyPolicyReadsBackConcretelyInEveryState(t *testing.T) {
	// The attribute is Optional+Computed over a NOT NULL column, so a null
	// read-back is the inconsistent-result shape. All three values are asserted
	// because a read-back hardcoded to the server default would pass on
	// "strict" alone -- which is the value most fixtures would reach for.
	ctx := context.Background()

	for _, policy := range validators.SSHHostKeyPolicies {
		t.Run(fmt.Sprintf("server says %s", policy), func(t *testing.T) {
			var m workspaceModel
			ws := &terrapod.Workspace{ID: "ws-a", Name: "a", SSHHostKeyPolicy: policy}
			if diags := readWorkspaceIntoModel(ctx, ws, &m); diags.HasError() {
				t.Fatalf("read: %v", diags)
			}
			got := m.SSHHostKeyPolicy
			if got.IsNull() || got.IsUnknown() {
				t.Fatalf("ssh_host_key_policy read back as %v, want a concrete value", got)
			}
			if got.ValueString() != policy {
				t.Errorf("ssh_host_key_policy = %q, want %q", got.ValueString(), policy)
			}
		})
	}
}

// TestTheRetiredBooleanIsGoneFromTheSchema is the half a rename needs that a
// round-trip test cannot give: a schema carrying BOTH attributes would pass
// every test above while offering a practitioner a boolean the server no longer
// accepts anywhere.
func TestTheRetiredBooleanIsGoneFromTheSchema(t *testing.T) {
	var resp resource.SchemaResponse
	NewResource().Schema(context.Background(), resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema build error: %v", resp.Diagnostics)
	}
	for name := range resp.Schema.Attributes {
		if strings.Contains(name, "host_key_checking") {
			t.Errorf("attribute %q is still offered: #2592 replaced it with "+
				"ssh_host_key_policy and the server accepts it on no surface", name)
		}
	}
}
