package autodiscovery_rule

import (
	"context"
	"sort"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

func audMap(t *testing.T, entries map[string][]string) types.Map {
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

func sortedAudKeys(m map[string][]string) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// The rule templates oidc_audiences onto the workspaces it creates (#1901), so
// a workspace autodiscovery stands up inherits the cloud identity override the
// rule declares instead of silently coming up with none. The omitted case
// matters as much as the set one: a rule that does not mention it must leave
// the server's value alone, not plan it as empty.
func TestRuleOIDCAudiencesAttrs(t *testing.T) {
	cases := []struct {
		name    string
		val     types.Map
		want    map[string][]string
		present bool
	}{
		{
			// An alias is part of the KEY. `aws` and `aws.west` are two
			// independent provider configurations that share a prefix, and
			// anything splitting on the dot would merge them.
			"audiences templated onto new workspaces, one target aliased",
			audMap(t, map[string][]string{
				"aws":      {"sts.example.com", "api://example-exchange"},
				"aws.west": {"sts.example.com"},
			}),
			map[string][]string{
				"aws":      {"sts.example.com", "api://example-exchange"},
				"aws.west": {"sts.example.com"},
			},
			true,
		},
		{
			"explicitly empty — new workspaces take the deployment catalogue alone",
			audMap(t, map[string][]string{}), map[string][]string{}, true,
		},
		{
			"omitted — the rule's existing value stands",
			types.MapNull(audienceElemType), nil, false,
		},
		{
			"unknown (create, no prior state)",
			types.MapUnknown(audienceElemType), nil, false,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			m := &autodiscoveryRuleModel{OIDCAudiences: tc.val}
			attrs := buildAutodiscoveryRuleAttrs(m)

			raw, ok := attrs["oidc-audiences"]
			if ok != tc.present {
				t.Fatalf("oidc-audiences present=%v (value %v), want present=%v", ok, raw, tc.present)
			}
			if !tc.present {
				return
			}
			got, isMap := raw.(map[string][]string)
			if !isMap {
				t.Fatalf("oidc-audiences is %T, want map[string][]string", raw)
			}
			gk, wk := sortedAudKeys(got), sortedAudKeys(tc.want)
			if len(gk) != len(wk) {
				t.Fatalf("oidc-audiences keys = %v, want %v", gk, wk)
			}
			for i := range wk {
				if gk[i] != wk[i] {
					t.Fatalf("oidc-audiences keys = %v, want %v", gk, wk)
				}
			}
			for _, k := range wk {
				g, w := got[k], tc.want[k]
				if len(g) != len(w) {
					t.Errorf("oidc-audiences[%s] = %q, want %q", k, g, w)
					continue
				}
				for i := range w {
					if g[i] != w[i] {
						t.Errorf("oidc-audiences[%s] = %q, want %q (entry %d differs)", k, g, w, i)
						break
					}
				}
			}
		})
	}
}

// Read-back: verbatim and in order, for the same reason the workspace resource
// cares — the provider writes the server's response into state, so any
// normalisation here makes a plan disagree with its own apply.
//
// It is a FULL read, unlike the workspace resource's selective one. A rule is a
// template, not a workspace: the server stores and returns the rule's own map
// with nothing merged into it, so every key that comes back is one this
// configuration wrote. The deployment's audience catalogue is merged per
// workspace at mint time, downstream of here.
func TestRuleOIDCAudiencesRoundTrips(t *testing.T) {
	ctx := context.Background()

	var m autodiscoveryRuleModel
	res := ruleResource(t, map[string]any{
		"oidc-audiences": map[string][]string{
			"aws":      {"sts.example.com", "api://example-exchange"},
			"aws.west": {"sts.example.com"},
		},
	})
	if diags := readAutodiscoveryRuleIntoModel(ctx, res, &m); diags.HasError() {
		t.Fatalf("read: %v", diags)
	}
	if m.OIDCAudiences.IsNull() || m.OIDCAudiences.IsUnknown() {
		t.Fatalf("oidc_audiences read back as %v, want a concrete map", m.OIDCAudiences)
	}
	elems := m.OIDCAudiences.Elements()
	if len(elems) != 2 {
		t.Fatalf("oidc_audiences read back %d keys, want 2: %v", len(elems), elems)
	}
	if _, ok := elems["aws.west"]; !ok {
		t.Error("the aliased `aws.west` key was not read back as its own key")
	}
	l, ok := elems["aws"].(types.List)
	if !ok {
		t.Fatalf("oidc_audiences[aws] is %T, want types.List", elems["aws"])
	}
	items := l.Elements()
	if len(items) != 2 {
		t.Fatalf("oidc_audiences[aws] read back %d audiences, want 2", len(items))
	}
	if got := items[0].(types.String).ValueString(); got != "sts.example.com" {
		t.Errorf("first audience = %q, want sts.example.com", got)
	}
	if got := items[1].(types.String).ValueString(); got != "api://example-exchange" {
		t.Errorf("second audience = %q, want api://example-exchange", got)
	}
}

// A rule holding nothing reads back null, so a config that never set the
// attribute produces no spurious diff against the server's empty map.
func TestRuleOIDCAudiencesReadsAnEmptyMapAsNull(t *testing.T) {
	ctx := context.Background()

	var m autodiscoveryRuleModel
	res := ruleResource(t, map[string]any{"oidc-audiences": map[string][]string{}})
	if diags := readAutodiscoveryRuleIntoModel(ctx, res, &m); diags.HasError() {
		t.Fatalf("read: %v", diags)
	}
	if !m.OIDCAudiences.IsNull() {
		t.Errorf("oidc_audiences = %v, want null for a rule that templates nothing",
			m.OIDCAudiences)
	}
}

func TestRuleOIDCAudiencesIsOptionalAndComputed(t *testing.T) {
	var resp resource.SchemaResponse
	NewResource().Schema(context.Background(), resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema build error: %v", resp.Diagnostics)
	}

	a, ok := resp.Schema.Attributes["oidc_audiences"]
	if !ok {
		t.Fatal("oidc_audiences attribute is missing from the terrapod_autodiscovery_rule schema")
	}
	if !a.IsOptional() || !a.IsComputed() {
		t.Errorf("oidc_audiences must be Optional+Computed (#684); optional=%v computed=%v",
			a.IsOptional(), a.IsComputed())
	}
	// A map of lists, matching the workspace resource and the wire. A rule that
	// templated a different shape from the workspace it creates would be
	// accepted here and rejected at materialisation time.
	ma, ok := a.(schema.MapAttribute)
	if !ok {
		t.Fatalf("oidc_audiences is %T, want schema.MapAttribute", a)
	}
	if ma.ElementType != (types.ListType{ElemType: types.StringType}) {
		t.Errorf("oidc_audiences element type is %v, want a list of strings", ma.ElementType)
	}
}
