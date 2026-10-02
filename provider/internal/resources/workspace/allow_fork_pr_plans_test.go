package workspace

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"
	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// allow_fork_pr_plans decides whether a pull request opened from a fork gets a
// speculative plan — the only path by which a contributor who cannot merge ever
// runs code against the workspace's credentials (GHSA-gp5w-76rw-c452). It is
// Optional so an operator can turn it on for a workspace holding nothing worth
// taking, and Computed so the overwhelming majority of configs, which never
// mention it, keep the server's value instead of planning null (#684).
func TestAllowForkPRPlansIsOptionalAndComputed(t *testing.T) {
	var resp resource.SchemaResponse
	NewResource().Schema(context.Background(), resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema build error: %v", resp.Diagnostics)
	}

	attr, ok := resp.Schema.Attributes["allow_fork_pr_plans"]
	if !ok {
		t.Fatal("allow_fork_pr_plans attribute is missing")
	}
	if !attr.IsOptional() {
		t.Error("allow_fork_pr_plans must be Optional — otherwise it cannot be turned on from HCL")
	}
	if !attr.IsComputed() {
		t.Error("allow_fork_pr_plans must be Computed — configs that omit it would plan a spurious diff")
	}
	if desc := attr.GetDescription(); desc == "" {
		t.Error("allow_fork_pr_plans needs a description: the default is a security posture, not a preference")
	}
}

// The *bool on the request structs exists so an explicit `false` is
// distinguishable from "not set". Dropping it is the dangerous direction: an
// operator writing `allow_fork_pr_plans = false` to turn fork plans back OFF
// would get a silent no-op, and the workspace would go on planning fork pull
// requests with its credentials.
func TestAllowForkPRPlansReachesTheWire(t *testing.T) {
	ctx := context.Background()

	cases := []struct {
		name string
		val  types.Bool
		want *bool
	}{
		{"explicitly true", types.BoolValue(true), ptr(true)},
		{"explicitly false", types.BoolValue(false), ptr(false)},
		{"omitted — leave the server's value alone", types.BoolNull(), nil},
		{"unknown (create, no prior state)", types.BoolUnknown(), nil},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceModel
			m.AllowForkPRPlans = tc.val

			create, diags := buildCreateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("create: %v", diags)
			}
			checkBoolPtr(t, "create", create.AllowForkPRPlans, tc.want)

			update, diags := buildUpdateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("update: %v", diags)
			}
			checkBoolPtr(t, "update", update.AllowForkPRPlans, tc.want)
		})
	}
}

// The server answers the attribute unconditionally, so the read-back must pin a
// concrete value — a null here is the #684 "inconsistent result after apply".
func TestReadWorkspaceIntoModelAllowForkPRPlans(t *testing.T) {
	ctx := context.Background()

	for _, want := range []bool{true, false} {
		var m workspaceModel
		ws := &terrapod.Workspace{ID: "ws-a", Name: "a", AllowForkPRPlans: want}
		if diags := readWorkspaceIntoModel(ctx, ws, &m); diags.HasError() {
			t.Fatalf("read: %v", diags)
		}
		if m.AllowForkPRPlans.IsNull() || m.AllowForkPRPlans.IsUnknown() {
			t.Fatalf("allow_fork_pr_plans read back as %v, want a concrete value", m.AllowForkPRPlans)
		}
		if got := m.AllowForkPRPlans.ValueBool(); got != want {
			t.Errorf("allow_fork_pr_plans = %v, want %v", got, want)
		}
	}
}

func ptr(b bool) *bool { return &b }

func checkBoolPtr(t *testing.T, phase string, got, want *bool) {
	t.Helper()
	switch {
	case want == nil && got != nil:
		t.Errorf("%s sent allow-fork-pr-plans=%v, want it omitted", phase, *got)
	case want != nil && got == nil:
		t.Errorf("%s dropped allow-fork-pr-plans=%v", phase, *want)
	case want != nil && got != nil && *got != *want:
		t.Errorf("%s sent allow-fork-pr-plans=%v, want %v", phase, *got, *want)
	}
}
