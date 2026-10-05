package autodiscovery_rule

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

func audList(vals ...string) types.List {
	elems := make([]attr.Value, 0, len(vals))
	for _, v := range vals {
		elems = append(elems, types.StringValue(v))
	}
	return types.ListValueMust(types.StringType, elems)
}

// The rule templates oidc_audiences onto the workspaces it creates (#1901), so
// a workspace autodiscovery stands up inherits the cloud identity the rule
// declares instead of silently coming up with none. The omitted case matters as
// much as the set one: a rule that does not mention it must leave the server's
// value alone, not plan it as empty.
func TestRuleOIDCAudiencesAttrs(t *testing.T) {
	cases := []struct {
		name    string
		val     types.List
		want    []string
		present bool
	}{
		{"audiences templated onto new workspaces",
			audList("sts.example.com", "api://example-exchange"),
			[]string{"sts.example.com", "api://example-exchange"}, true},
		{"explicitly empty — new workspaces mint nothing",
			audList(), []string{}, true},
		{"omitted — the server's default stands",
			types.ListNull(types.StringType), nil, false},
		{"unknown (create, no prior state)",
			types.ListUnknown(types.StringType), nil, false},
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
			got, isSlice := raw.([]string)
			if !isSlice {
				t.Fatalf("oidc-audiences is %T, want []string", raw)
			}
			if len(got) != len(tc.want) {
				t.Fatalf("oidc-audiences = %q, want %q", got, tc.want)
			}
			for i := range tc.want {
				if got[i] != tc.want[i] {
					t.Errorf("oidc-audiences = %q, want %q", got, tc.want)
					return
				}
			}
		})
	}
}

// Read-back: verbatim and in order, for the same reason the workspace resource
// cares — the provider writes the server's response into state, so any
// normalisation here makes a plan disagree with its own apply.
func TestRuleOIDCAudiencesRoundTrips(t *testing.T) {
	ctx := context.Background()

	var m autodiscoveryRuleModel
	res := ruleResource(t, map[string]any{
		"oidc-audiences": []string{"sts.example.com", "api://example-exchange"},
	})
	if diags := readAutodiscoveryRuleIntoModel(ctx, res, &m); diags.HasError() {
		t.Fatalf("read: %v", diags)
	}
	if m.OIDCAudiences.IsNull() || m.OIDCAudiences.IsUnknown() {
		t.Fatalf("oidc_audiences read back as %v, want a concrete list", m.OIDCAudiences)
	}
	elems := m.OIDCAudiences.Elements()
	if len(elems) != 2 {
		t.Fatalf("oidc_audiences read back with %d elements, want 2", len(elems))
	}
	if got := elems[0].(types.String).ValueString(); got != "sts.example.com" {
		t.Errorf("first audience = %q, want sts.example.com", got)
	}
	if got := elems[1].(types.String).ValueString(); got != "api://example-exchange" {
		t.Errorf("second audience = %q, want api://example-exchange", got)
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
}
