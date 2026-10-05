package workspace

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	rschema "github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/tfsdk"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-go/tftypes"
	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// oidc_audiences is the per-workspace cloud identity opt-in (#1901): the
// audiences a run's identity token is minted for. Nothing about it is
// cloud-specific — Terrapod mints an OIDC JWT and the runner writes it to a
// file; which provider reads that file is the operator's own configuration, and
// an audience is an opaque string the federation target itself named.
//
// It is an ordinary mutable setting: Optional so it can be set from HCL,
// Computed so a config that never mentions it keeps the server's value instead
// of planning null (#684), and NOT replace-forcing.

func oidcAudiencesAttr(t *testing.T) rschema.ListAttribute {
	t.Helper()
	var resp resource.SchemaResponse
	NewResource().Schema(context.Background(), resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema build error: %v", resp.Diagnostics)
	}
	raw, ok := resp.Schema.Attributes["oidc_audiences"]
	if !ok {
		t.Fatal("oidc_audiences attribute is missing from the terrapod_workspace schema")
	}
	la, ok := raw.(rschema.ListAttribute)
	if !ok {
		t.Fatalf("oidc_audiences is %T, want rschema.ListAttribute", raw)
	}
	return la
}

func TestOIDCAudiencesIsOptionalAndComputed(t *testing.T) {
	la := oidcAudiencesAttr(t)

	if !la.IsOptional() {
		t.Error("oidc_audiences must be Optional — otherwise it cannot be set from HCL")
	}
	if !la.IsComputed() {
		t.Error("oidc_audiences must be Computed (#684): a config that omits it would plan null " +
			"against the server's non-null list and the apply would fail with " +
			"\"Provider produced inconsistent result after apply\"")
	}
	if la.ElementType != types.StringType {
		t.Errorf("oidc_audiences element type is %v, want string", la.ElementType)
	}
	if la.GetDescription() == "" {
		t.Error("oidc_audiences needs a description: an operator has to know an audience is the " +
			"federation target's own opaque string, and that the empty list is the opt-out")
	}
}

// Driven, not inspected: each plan modifier is run against a real state-to-plan
// change and asked whether it forces replacement.
//
// The State/Plan/Config have to carry a NON-NULL raw value and the state and
// plan values have to DIFFER, or the framework's requires-replace modifier
// returns early (it reads a null raw as create-or-destroy, and equal values as
// no change) -- so a request built from zero-valued tfsdk types passes this
// test whether or not the attribute forces replacement. Measured against the
// framework, not assumed.
//
// Why it matters: changing which audiences a workspace mints for takes effect
// on its next run. Replacing the workspace would destroy its state and its run
// history for a settings change.
func TestOIDCAudiencesDoesNotForceReplacement(t *testing.T) {
	ctx := context.Background()
	la := oidcAudiencesAttr(t)

	if len(la.PlanModifiers) == 0 {
		t.Fatal("oidc_audiences has no plan modifiers; it needs UseStateForUnknown (#684) at minimum")
	}

	sch := rschema.Schema{Attributes: map[string]rschema.Attribute{
		"oidc_audiences": rschema.ListAttribute{
			ElementType: types.StringType, Optional: true, Computed: true,
		},
	}}
	objType := tftypes.Object{AttributeTypes: map[string]tftypes.Type{
		"oidc_audiences": tftypes.List{ElementType: tftypes.String},
	}}
	raw := func(v string) tftypes.Value {
		return tftypes.NewValue(objType, map[string]tftypes.Value{
			"oidc_audiences": tftypes.NewValue(
				tftypes.List{ElementType: tftypes.String},
				[]tftypes.Value{tftypes.NewValue(tftypes.String, v)},
			),
		})
	}

	before, after := strList("sts.example.com"), strList("api://example-exchange")

	for i, pm := range la.PlanModifiers {
		req := planmodifier.ListRequest{
			Path:        path.Root("oidc_audiences"),
			StateValue:  before,
			PlanValue:   after,
			ConfigValue: after,
			State:       tfsdk.State{Schema: sch, Raw: raw("sts.example.com")},
			Plan:        tfsdk.Plan{Schema: sch, Raw: raw("api://example-exchange")},
			Config:      tfsdk.Config{Schema: sch, Raw: raw("api://example-exchange")},
		}
		resp := &planmodifier.ListResponse{PlanValue: req.PlanValue}
		pm.PlanModifyList(ctx, req, resp)
		if resp.RequiresReplace {
			t.Errorf("plan modifier %d (%s) forces replacement when the audiences change; this "+
				"is an ordinary setting that takes effect on the next run, and replacing the "+
				"workspace would destroy its state and run history",
				i, pm.Description(ctx))
		}
	}
}

// Writing the audiences down IS the opt-in, and clearing them is the opt-OUT,
// so both have to reach the wire — and both have to stay distinguishable from
// "the config does not mention this attribute".
//
// The empty-list case is the dangerous direction: an operator who deletes every
// audience to stop a workspace minting a cloud identity must not get a silent
// no-op that leaves it minting.
func TestOIDCAudiencesReachesTheWire(t *testing.T) {
	ctx := context.Background()

	cases := []struct {
		name string
		val  types.List
		want []string // nil means the attribute must be omitted entirely
	}{
		{"two audiences", strList("sts.example.com", "api://example-exchange"),
			[]string{"sts.example.com", "api://example-exchange"}},
		{"explicitly empty — opts the workspace back out", strList(), []string{}},
		{"omitted — leave the server's value alone", types.ListNull(types.StringType), nil},
		{"unknown (create, no prior state)", types.ListUnknown(types.StringType), nil},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceModel
			m.OIDCAudiences = tc.val

			create, diags := buildCreateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("create: %v", diags)
			}
			checkAudiences(t, "create", create.OIDCAudiences, tc.want)

			update, diags := buildUpdateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("update: %v", diags)
			}
			checkAudiences(t, "update", update.OIDCAudiences, tc.want)
		})
	}
}

// Read-back, driven from the state the real caller actually passes in.
//
// On a create or update the model comes from the PLAN, where an
// Optional+Computed attribute reads Unknown. So every case here starts Unknown
// — which is also what makes the test honest: an Unknown left in the model
// after Read is a framework error ("Provider returned invalid result object"),
// so asserting it is concrete catches a read-back that was simply never
// written, which is the `plan-expiry-seconds` defect.
//
// Verbatim matters because the provider writes the response into state: any
// normalisation here — trimming, lower-casing, sorting — makes a plan disagree
// with its own apply, which the framework reports as "Provider produced
// inconsistent result after apply" and which re-running never fixes.
func TestReadWorkspaceIntoModelOIDCAudiences(t *testing.T) {
	ctx := context.Background()

	cases := []struct {
		name   string
		prior  types.List
		server []string
		want   []string // nil means the result must be a null list
	}{
		{
			"the server's value wins over a stale prior",
			strList("stale.example.com"),
			[]string{"sts.example.com", "api://example-exchange"},
			[]string{"sts.example.com", "api://example-exchange"},
		},
		{
			// Deliberately ugly values: whitespace and mixed case that any
			// well-meaning canonicalisation would "fix".
			"audiences come back byte-for-byte",
			types.ListUnknown(types.StringType),
			[]string{"STS.Example.com ", "api://Example-Exchange"},
			[]string{"STS.Example.com ", "api://Example-Exchange"},
		},
		{
			"order is preserved",
			types.ListUnknown(types.StringType),
			[]string{"b.example.com", "a.example.com"},
			[]string{"b.example.com", "a.example.com"},
		},
		{
			"a declared empty list stays empty, not null",
			strList(),
			[]string{},
			[]string{},
		},
		{
			// The config omitted the attribute and the workspace mints nothing.
			// The server still SENDS the key (the serializer emits a list
			// unconditionally), so this arrives as a non-nil empty slice — and
			// without the null-preservation branch it would read back as an
			// empty list against a null config and diff on every plan.
			"omitted config against an empty server value stays null",
			types.ListNull(types.StringType),
			[]string{},
			nil,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceModel
			m.OIDCAudiences = tc.prior
			ws := &terrapod.Workspace{ID: "ws-a", Name: "my-workspace", OIDCAudiences: tc.server}
			if diags := readWorkspaceIntoModel(ctx, ws, &m); diags.HasError() {
				t.Fatalf("read: %v", diags)
			}

			// Unconditional: an Unknown surviving Read means the attribute was
			// never written, and Terraform rejects an unknown in final state.
			if m.OIDCAudiences.IsUnknown() {
				t.Fatal("oidc_audiences is still Unknown after Read — it is never decoded, so " +
					"every consumer reads a value the server did not send")
			}

			if tc.want == nil {
				if !m.OIDCAudiences.IsNull() {
					t.Errorf("oidc_audiences = %v, want null", m.OIDCAudiences)
				}
				return
			}
			if m.OIDCAudiences.IsNull() {
				t.Fatalf("oidc_audiences read back null, want %q", tc.want)
			}
			got := listStrings(t, m.OIDCAudiences)
			if len(got) != len(tc.want) {
				t.Fatalf("oidc_audiences = %q, want %q", got, tc.want)
			}
			for i := range tc.want {
				if got[i] != tc.want[i] {
					t.Errorf("oidc_audiences = %q, want %q (entry %d differs)", got, tc.want, i)
					return
				}
			}
		})
	}
}

// ── helpers ──────────────────────────────────────────────────────────

func strList(vals ...string) types.List {
	elems := make([]attr.Value, 0, len(vals))
	for _, v := range vals {
		elems = append(elems, types.StringValue(v))
	}
	return types.ListValueMust(types.StringType, elems)
}

func listStrings(t *testing.T, l types.List) []string {
	t.Helper()
	out := make([]string, 0, len(l.Elements()))
	for _, e := range l.Elements() {
		s, ok := e.(types.String)
		if !ok {
			t.Fatalf("list element %v is %T, want types.String", e, e)
		}
		out = append(out, s.ValueString())
	}
	return out
}

func checkAudiences(t *testing.T, phase string, got, want []string) {
	t.Helper()
	switch {
	case want == nil && got != nil:
		t.Errorf("%s sent oidc-audiences=%q, want it omitted so the server's value is left alone",
			phase, got)
	case want == nil:
		return
	case got == nil:
		t.Errorf("%s dropped oidc-audiences=%q entirely, so it never reaches the server", phase, want)
	case len(got) != len(want):
		t.Errorf("%s sent oidc-audiences=%q, want %q", phase, got, want)
	default:
		for i := range want {
			if got[i] != want[i] {
				t.Errorf("%s sent oidc-audiences=%q, want %q", phase, got, want)
				return
			}
		}
	}
}
