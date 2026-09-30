package workspace

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// The Terraform-engine default is the half of this that must NOT move. It was
// an unconditional schema default before #1911 and is a plan modifier after,
// and the observable result has to be identical: a configuration that omits
// `execution_backend` plans "terraform", on create and on update, whether
// `engine` is written out, left to the server, or absent because the
// configuration predates the attribute.
func TestExecutionBackendKeepsTheTerraformDefault(t *testing.T) {
	cases := []struct {
		name   string
		engine types.String
	}{
		{"engine explicitly terraform", types.StringValue("terraform")},
		{"engine omitted (null in plan)", types.StringNull()},
		{"engine computed on create (unknown)", types.StringUnknown()},
		{"an engine that is not pulumi", types.StringValue("ansible")},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := executionBackendPlan(types.StringNull(), tc.engine)
			if !ok {
				t.Fatalf("no default planned for %v — the Terraform default was dropped", tc.engine)
			}
			if got != "terraform" {
				t.Errorf("planned %q, want \"terraform\"", got)
			}
		})
	}
}

// On Pulumi the provider must assert nothing. The attribute names a choice
// between the tofu and terraform binaries; Pulumi has one, so a planned value
// here is the provider inventing a setting the operator never wrote about a
// decision their engine does not offer.
func TestExecutionBackendPlansNothingForPulumi(t *testing.T) {
	if got, ok := executionBackendPlan(types.StringNull(), types.StringValue("pulumi")); ok {
		t.Errorf("planned %q on a Pulumi workspace; want the server to assign", got)
	}
}

// A configured value stands on every engine, because the server accepts one on
// every engine. Overriding it would make the provider fight a configuration the
// API applies cleanly.
func TestExecutionBackendNeverOverridesAConfiguredValue(t *testing.T) {
	for _, engine := range []string{"terraform", "pulumi"} {
		if got, ok := executionBackendPlan(types.StringValue("tofu"), types.StringValue(engine)); ok {
			t.Errorf("engine %s: overrode a configured value with %q", engine, got)
		}
	}
}

// The warning fires only where the configuration is genuinely recording a
// meaningless choice — not on Terraform, and not merely because the attribute
// is absent.
func TestExecutionBackendMeaninglessOnlyWhenSetOnPulumi(t *testing.T) {
	cases := []struct {
		name   string
		config types.String
		engine types.String
		want   bool
	}{
		{"set on pulumi", types.StringValue("tofu"), types.StringValue("pulumi"), true},
		{"set on terraform", types.StringValue("tofu"), types.StringValue("terraform"), false},
		{"unset on pulumi", types.StringNull(), types.StringValue("pulumi"), false},
		{"unknown on pulumi", types.StringUnknown(), types.StringValue("pulumi"), false},
		{"set while the engine is unknown", types.StringValue("tofu"), types.StringUnknown(), false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := executionBackendIsMeaningless(tc.config, tc.engine); got != tc.want {
				t.Errorf("executionBackendIsMeaningless = %v, want %v", got, tc.want)
			}
		})
	}
}

// The schema must carry the engine-aware modifier and must NOT carry an
// unconditional default — a `Default` would run first and re-impose
// "terraform" on Pulumi before the modifier ever saw the plan, restoring the
// bug while every unit test above still passed.
func TestExecutionBackendSchemaHasNoUnconditionalDefault(t *testing.T) {
	var resp resource.SchemaResponse
	NewResource().(*workspaceResource).Schema(context.Background(), resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema: %v", resp.Diagnostics)
	}
	attr, ok := resp.Schema.Attributes["execution_backend"].(schema.StringAttribute)
	if !ok {
		t.Fatal("execution_backend is not a string attribute")
	}
	if attr.Default != nil {
		t.Error("execution_backend still has an unconditional Default; it must be engine-aware")
	}
	var found bool
	for _, pm := range attr.PlanModifiers {
		if _, ok := pm.(engineAwareBackendDefault); ok {
			found = true
		}
	}
	if !found {
		t.Error("execution_backend has no engineAwareBackendDefault plan modifier")
	}
	// Optional+Computed is what lets the server fill the Pulumi case in.
	if !attr.IsOptional() || !attr.IsComputed() {
		t.Error("execution_backend must stay optional+computed")
	}
}
