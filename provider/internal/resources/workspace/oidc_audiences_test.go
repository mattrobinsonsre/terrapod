package workspace

import (
	"context"
	"sort"
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

// oidc_audiences is the per-workspace cloud identity override (#1901): a map
// from the provider configuration a run's identity token is for — `aws`, or
// `aws.west` for one aliased configuration — to the audiences that token is
// minted with. Nothing about it is cloud-specific: Terrapod mints an OIDC JWT
// per key and the runner writes it to a file; which provider reads that file is
// the operator's own configuration, and an audience is an opaque string the
// federation target itself named.
//
// It is an ordinary mutable setting: Optional so it can be set from HCL,
// Computed so a config that never mentions it keeps the server's value instead
// of planning null (#684), and NOT replace-forcing.
//
// The end-to-end plan/apply/refresh/plan cycle — where the selective read and
// the merged-view problem actually live — is driven through the real protocol
// server in internal/provider/oidc_audiences_cycle_test.go. What is here is the
// unit half: the schema, the request shape, and the read-back in isolation.

func oidcAudiencesAttr(t *testing.T) rschema.MapAttribute {
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
	ma, ok := raw.(rschema.MapAttribute)
	if !ok {
		t.Fatalf("oidc_audiences is %T, want rschema.MapAttribute", raw)
	}
	return ma
}

func TestOIDCAudiencesIsOptionalAndComputed(t *testing.T) {
	ma := oidcAudiencesAttr(t)

	if !ma.IsOptional() {
		t.Error("oidc_audiences must be Optional — otherwise it cannot be set from HCL")
	}
	if !ma.IsComputed() {
		t.Error("oidc_audiences must be Computed (#684): a config that omits it would plan null " +
			"against the server's non-null map and the apply would fail with " +
			"\"Provider produced inconsistent result after apply\"")
	}
	// A list per key, never a bare string. One audience is still a one-element
	// list; a list of several means "these are interchangeable for this
	// target", which some federation targets refuse outright — so collapsing
	// the common case to a scalar would make the uncommon one inexpressible.
	if ma.ElementType != (types.ListType{ElemType: types.StringType}) {
		t.Errorf("oidc_audiences element type is %v, want a list of strings", ma.ElementType)
	}
	if ma.GetDescription() == "" {
		t.Error("oidc_audiences needs a description: an operator has to know a key is a provider " +
			"configuration name, that an alias is part of the key, and that this is an " +
			"override over the deployment's own catalogue rather than the effective set")
	}
}

// The description has to say that a key is removed rather than emptied, because
// the alternative — an empty list under a key — is what an operator naturally
// reaches for and the server refuses it (422). A rejected apply is recoverable;
// an operator who cannot work out why is the cost this guards against.
func TestOIDCAudiencesDescriptionExplainsRemovingAKey(t *testing.T) {
	desc := oidcAudiencesAttr(t).GetDescription()
	for _, want := range []string{"REMOVING the key", "empty list under a key is refused"} {
		if !contains(desc, want) {
			t.Errorf("description must mention %q so an operator knows how to say "+
				"\"no audiences for this target\"; got: %s", want, desc)
		}
	}
	// And that it is an override, not the effective set — otherwise a reader
	// takes the absence of an inherited key in state as the workspace not
	// having it.
	if !contains(desc, "OVERRIDE") {
		t.Errorf("description must say this is an override over the deployment's catalogue, "+
			"not the effective merged set; got: %s", desc)
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
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
	ma := oidcAudiencesAttr(t)

	if len(ma.PlanModifiers) == 0 {
		t.Fatal("oidc_audiences has no plan modifiers; it needs UseStateForUnknown (#684) at minimum")
	}

	sch := rschema.Schema{Attributes: map[string]rschema.Attribute{
		"oidc_audiences": rschema.MapAttribute{
			ElementType: audienceElemType, Optional: true, Computed: true,
		},
	}}
	tfList := tftypes.List{ElementType: tftypes.String}
	objType := tftypes.Object{AttributeTypes: map[string]tftypes.Type{
		"oidc_audiences": tftypes.Map{ElementType: tfList},
	}}
	raw := func(v string) tftypes.Value {
		return tftypes.NewValue(objType, map[string]tftypes.Value{
			"oidc_audiences": tftypes.NewValue(tftypes.Map{ElementType: tfList},
				map[string]tftypes.Value{
					"aws": tftypes.NewValue(tfList, []tftypes.Value{
						tftypes.NewValue(tftypes.String, v),
					}),
				}),
		})
	}

	before := audienceMapValue(t, map[string][]string{"aws": {"sts.example.com"}})
	after := audienceMapValue(t, map[string][]string{"aws": {"api://example-exchange"}})

	for i, pm := range ma.PlanModifiers {
		req := planmodifier.MapRequest{
			Path:        path.Root("oidc_audiences"),
			StateValue:  before,
			PlanValue:   after,
			ConfigValue: after,
			State:       tfsdk.State{Schema: sch, Raw: raw("sts.example.com")},
			Plan:        tfsdk.Plan{Schema: sch, Raw: raw("api://example-exchange")},
			Config:      tfsdk.Config{Schema: sch, Raw: raw("api://example-exchange")},
		}
		resp := &planmodifier.MapResponse{PlanValue: req.PlanValue}
		pm.PlanModifyMap(ctx, req, resp)
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
// The empty-map case is the dangerous direction: an operator who deletes every
// override to stop a workspace minting against them must not get a silent
// no-op that leaves it minting.
func TestOIDCAudiencesReachesTheWire(t *testing.T) {
	ctx := context.Background()

	cases := []struct {
		name string
		val  types.Map
		want map[string][]string // nil means the attribute must be omitted entirely
	}{
		{
			// An alias is part of the key, so `aws` and `aws.west` travel as
			// two independent keys. Anything that split on the dot would merge
			// them and silently give one provider configuration the other's
			// audiences.
			"two provider configurations, one of them aliased",
			audienceMapValue(t, map[string][]string{
				"aws":      {"sts.example.com"},
				"aws.west": {"sts.example.com"},
			}),
			map[string][]string{"aws": {"sts.example.com"}, "aws.west": {"sts.example.com"}},
		},
		{
			"several interchangeable audiences for one target, in order",
			audienceMapValue(t, map[string][]string{"vault": {"https://b", "https://a"}}),
			map[string][]string{"vault": {"https://b", "https://a"}},
		},
		{
			"explicitly empty — drops every override",
			audienceMapValue(t, map[string][]string{}),
			map[string][]string{},
		},
		{
			"omitted — leave the server's value alone",
			types.MapNull(audienceElemType), nil,
		},
		{
			"unknown (create, no prior state)",
			types.MapUnknown(audienceElemType), nil,
		},
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

// The selective read in isolation (#1901). The cycle test proves the property
// that matters — that an inherited key never reaches state and the next plan is
// empty — but it cannot enumerate the shapes cheaply, so the table is here.
//
// On a create or update the model comes from the PLAN, where an
// Optional+Computed attribute reads Unknown. An Unknown left in the model after
// Read is a framework error ("Provider returned invalid result object"), so
// asserting it is concrete catches a read-back that was simply never written,
// which is the `plan-expiry-seconds` defect.
func TestReadWorkspaceIntoModelOIDCAudiences(t *testing.T) {
	ctx := context.Background()

	// The merged view the server answers with throughout: the configuration's
	// own keys plus one the deployment supplies.
	merged := map[string][]string{
		"aws":      {"sts.example.com"},
		"aws.west": {"sts.example.com"},
		"vault":    {"https://vault.example.com"},
	}

	cases := []struct {
		name  string
		prior types.Map
		// server is the MERGED map the API returns, which is wider than the
		// override that was written.
		server map[string][]string
		want   map[string][]string // nil means the result must be a null map
	}{
		{
			"only the keys this config owns are kept",
			audienceMapValue(t, map[string][]string{"aws": {"stale"}}),
			merged,
			map[string][]string{"aws": {"sts.example.com"}},
		},
		{
			"an aliased key is its own key, not folded into its unaliased sibling",
			audienceMapValue(t, map[string][]string{"aws.west": {"stale"}}),
			merged,
			map[string][]string{"aws.west": {"sts.example.com"}},
		},
		{
			// The server's value wins for an owned key — that is how drift on a
			// key the config manages gets detected.
			"the server's value wins over a stale prior",
			audienceMapValue(t, map[string][]string{"vault": {"stale.example.com"}}),
			merged,
			map[string][]string{"vault": {"https://vault.example.com"}},
		},
		{
			// Deliberately ugly values: whitespace and mixed case that any
			// well-meaning canonicalisation would "fix". The provider writes
			// this into state, so normalising makes a plan disagree with its
			// own apply — which Terraform core reports as "inconsistent result
			// after apply" and which re-running never fixes.
			"audiences come back byte-for-byte",
			audienceMapValue(t, map[string][]string{"aws": {"x"}}),
			map[string][]string{"aws": {"  STS.Example.com ", "api://Example-Exchange"}},
			map[string][]string{"aws": {"  STS.Example.com ", "api://Example-Exchange"}},
		},
		{
			"a declared empty map stays empty, not null",
			audienceMapValue(t, map[string][]string{}),
			merged,
			map[string][]string{},
		},
		{
			// Null config: the attribute is not managed here at all, so state
			// records nothing even though the server has a value. Adopting the
			// catalogue would send it back as this workspace's own override on
			// the next apply.
			"a config that does not declare it adopts nothing",
			types.MapNull(audienceElemType),
			merged,
			nil,
		},
		{
			// Unknown is what a create with no prior state plans, and it is
			// the same answer: nothing is owned yet.
			"unknown (create) adopts nothing",
			types.MapUnknown(audienceElemType),
			merged,
			nil,
		},
		{
			// The server took the write and did not store the key. Leaving it
			// OUT is deliberate: Terraform core then fails the apply with
			// "inconsistent result", which is the honest outcome.
			"a key the merge does not answer is left out rather than invented",
			audienceMapValue(t, map[string][]string{"aws": {"x"}, "gone": {"y"}}),
			map[string][]string{"aws": {"sts.example.com"}},
			map[string][]string{"aws": {"sts.example.com"}},
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
				t.Fatalf("oidc_audiences read back null, want %v", tc.want)
			}
			checkAudiences(t, "read", mapAudiences(t, m.OIDCAudiences), tc.want)
		})
	}
}

// ── helpers ──────────────────────────────────────────────────────────

func audienceMapValue(t *testing.T, entries map[string][]string) types.Map {
	t.Helper()
	elems := make(map[string]attr.Value, len(entries))
	for k, auds := range entries {
		items := make([]attr.Value, 0, len(auds))
		for _, a := range auds {
			items = append(items, types.StringValue(a))
		}
		elems[k] = types.ListValueMust(types.StringType, items)
	}
	return types.MapValueMust(audienceElemType, elems)
}

func mapAudiences(t *testing.T, m types.Map) map[string][]string {
	t.Helper()
	out := make(map[string][]string, len(m.Elements()))
	for k, v := range m.Elements() {
		l, ok := v.(types.List)
		if !ok {
			t.Fatalf("oidc_audiences[%s] is %T, want types.List", k, v)
		}
		auds := make([]string, 0, len(l.Elements()))
		for _, e := range l.Elements() {
			s, ok := e.(types.String)
			if !ok {
				t.Fatalf("oidc_audiences[%s] element %v is %T, want types.String", k, e, e)
			}
			auds = append(auds, s.ValueString())
		}
		out[k] = auds
	}
	return out
}

// checkAudiences compares two audience maps key by key and audience by
// audience, in order. `want == nil` asserts the attribute was omitted entirely,
// which is a different statement from an empty map.
func checkAudiences(t *testing.T, phase string, got, want map[string][]string) {
	t.Helper()
	switch {
	case want == nil && got != nil:
		t.Errorf("%s sent oidc-audiences=%v, want it omitted so the server's value is left alone",
			phase, got)
		return
	case want == nil:
		return
	case got == nil:
		t.Errorf("%s dropped oidc-audiences=%v entirely, so it never reaches the server", phase, want)
		return
	}
	gk, wk := sortedKeys(got), sortedKeys(want)
	if len(gk) != len(wk) {
		t.Errorf("%s sent oidc-audiences keys %v, want %v", phase, gk, wk)
		return
	}
	for i := range wk {
		if gk[i] != wk[i] {
			t.Errorf("%s sent oidc-audiences keys %v, want %v", phase, gk, wk)
			return
		}
	}
	for _, k := range wk {
		g, w := got[k], want[k]
		if len(g) != len(w) {
			t.Errorf("%s sent oidc-audiences[%s]=%q, want %q", phase, k, g, w)
			continue
		}
		for i := range w {
			if g[i] != w[i] {
				t.Errorf("%s sent oidc-audiences[%s]=%q, want %q (entry %d differs)",
					phase, k, g, w, i)
				break
			}
		}
	}
}

func sortedKeys(m map[string][]string) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
