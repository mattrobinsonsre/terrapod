package autodiscovery_rule

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// The engine version has two attribute names (#1559): `engine_version` is
// current, `terraform_version` is its deprecated alias. The alias keeps working
// indefinitely because the API keeps accepting the name go-tfe sends.

// ruleResource builds a JSON:API resource carrying the given attributes.
func ruleResource(t *testing.T, attrs map[string]any) *terrapod.Resource {
	t.Helper()
	raw := map[string]json.RawMessage{}
	for k, v := range attrs {
		b, err := json.Marshal(v)
		if err != nil {
			t.Fatalf("marshal %s: %v", k, err)
		}
		raw[k] = b
	}
	return &terrapod.Resource{ID: "adr-1", Type: "autodiscovery-rules", Attributes: raw}
}

func TestRuleEngineVersionAttrs_SendOneCanonicalKey(t *testing.T) {
	cases := []struct {
		name      string
		engine    types.String
		terraform types.String
		want      any // nil means the key must be absent
	}{
		{
			name:      "only the alias, as a config written before the rename has it",
			engine:    types.StringNull(),
			terraform: types.StringValue("1.11.4"),
			want:      "1.11.4",
		},
		{
			name:   "only the current attribute",
			engine: types.StringValue("1.12.0"), terraform: types.StringNull(),
			want: "1.12.0",
		},
		{
			name:   "both agreeing",
			engine: types.StringValue("1.12.0"), terraform: types.StringValue("1.12.0"),
			want: "1.12.0",
		},
		{
			// Neither attribute carries a client-side default any more, so an
			// omitted version means "let the server's column default apply"
			// rather than silently pinning a version the provider chose.
			name:   "neither set sends no version at all",
			engine: types.StringNull(), terraform: types.StringNull(),
			want: nil,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			attrs := buildAutodiscoveryRuleAttrs(&autodiscoveryRuleModel{
				EngineVersion:    tc.engine,
				TerraformVersion: tc.terraform,
			})
			got, has := attrs["engine-version"]
			if tc.want == nil {
				if has {
					t.Errorf("engine-version sent when neither attribute was set: %v", got)
				}
				return
			}
			if !has || got != tc.want {
				t.Errorf("engine-version = %v (present=%t), want %v", got, has, tc.want)
			}
			// Sending both keys risks a pair that disagrees, which is a 422.
			if _, has := attrs["terraform-version"]; has {
				t.Errorf("both keys sent: %+v", attrs)
			}
		})
	}
}

func TestRuleEngineVersionRead_FillsBothAttributes(t *testing.T) {
	cases := []struct {
		name  string
		attrs map[string]any
		want  string
	}{
		{
			name:  "current server sends both",
			attrs: map[string]any{"engine-version": "1.12.0", "terraform-version": "1.12.0"},
			want:  "1.12.0",
		},
		{
			name:  "older server sends only the original name",
			attrs: map[string]any{"terraform-version": "1.11.4"},
			want:  "1.11.4",
		},
		{
			name:  "no version at all",
			attrs: map[string]any{},
			want:  "",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			m := &autodiscoveryRuleModel{}
			diags := readAutodiscoveryRuleIntoModel(context.Background(), ruleResource(t, tc.attrs), m)
			if diags.HasError() {
				t.Fatalf("readAutodiscoveryRuleIntoModel: %v", diags)
			}
			// Both attributes take the one value, or a config using the other
			// name would see it change on every Read and never converge.
			if m.EngineVersion.ValueString() != tc.want {
				t.Errorf("EngineVersion = %q, want %q", m.EngineVersion.ValueString(), tc.want)
			}
			if m.TerraformVersion.ValueString() != tc.want {
				t.Errorf("TerraformVersion = %q, want %q", m.TerraformVersion.ValueString(), tc.want)
			}
		})
	}
}

func TestRuleEngineVersionValidation_RejectsADisagreeingPair(t *testing.T) {
	// The negative path: one version under two names cannot be two versions.
	// The server refuses the pair at apply; this catches it at plan.
	assertDisagreement := func(t *testing.T, engine, terraform types.String, wantError bool) {
		t.Helper()
		m := &autodiscoveryRuleModel{EngineVersion: engine, TerraformVersion: terraform}
		var got resource.ValidateConfigResponse
		validateRuleEngineVersionPair(m, &got)
		if has := got.Diagnostics.HasError(); has != wantError {
			t.Fatalf("HasError() = %t, want %t (%v)", has, wantError, got.Diagnostics)
		}
		if !wantError {
			return
		}
		detail := got.Diagnostics.Errors()[0].Detail()
		for _, want := range []string{"engine_version", "terraform_version"} {
			if !strings.Contains(detail, want) {
				t.Errorf("error detail does not mention %s: %s", want, detail)
			}
		}
	}

	t.Run("different values", func(t *testing.T) {
		assertDisagreement(t, types.StringValue("1.12.0"), types.StringValue("1.11.4"), true)
	})
	t.Run("same value is redundant but legal", func(t *testing.T) {
		assertDisagreement(t, types.StringValue("1.12.0"), types.StringValue("1.12.0"), false)
	})
	t.Run("only one set cannot disagree", func(t *testing.T) {
		assertDisagreement(t, types.StringValue("1.12.0"), types.StringNull(), false)
	})
	t.Run("an unknown value cannot be compared yet", func(t *testing.T) {
		assertDisagreement(t, types.StringValue("1.12.0"), types.StringUnknown(), false)
	})
}
