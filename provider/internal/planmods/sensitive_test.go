package planmods

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/tfsdk"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-go/tftypes"
)

// The three attributes the modifier reads, shaped as the real resources shape
// them.
func testSchema() schema.Schema {
	return schema.Schema{
		Attributes: map[string]schema.Attribute{
			"sensitive":    schema.BoolAttribute{Optional: true, Computed: true},
			"value_source": schema.StringAttribute{Optional: true, Computed: true},
			"category":     schema.StringAttribute{Required: true},
		},
	}
}

func objectType() tftypes.Object {
	return tftypes.Object{AttributeTypes: map[string]tftypes.Type{
		"sensitive":    tftypes.Bool,
		"value_source": tftypes.String,
		"category":     tftypes.String,
	}}
}

// run drives the modifier the way the framework does: `sensitive` already holds
// the value the default produced, and the plan carries the other two.
func run(t *testing.T, source, category tftypes.Value, sensitiveDefault bool, config types.Bool) planmodifier.BoolResponse {
	t.Helper()
	plan := tfsdk.Plan{
		Schema: testSchema(),
		Raw: tftypes.NewValue(objectType(), map[string]tftypes.Value{
			"sensitive":    tftypes.NewValue(tftypes.Bool, sensitiveDefault),
			"value_source": source,
			"category":     category,
		}),
	}
	req := planmodifier.BoolRequest{
		Path:        path.Root("sensitive"),
		Plan:        plan,
		ConfigValue: config,
		PlanValue:   types.BoolValue(sensitiveDefault),
	}
	resp := planmodifier.BoolResponse{PlanValue: req.PlanValue}
	SensitiveForSecretBearingVariable().PlanModifyBool(context.Background(), req, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("unexpected error diagnostics: %v", resp.Diagnostics)
	}
	return resp
}

func str(s string) tftypes.Value { return tftypes.NewValue(tftypes.String, s) }

// The defect this modifier exists for: the documented HCL omits `sensitive`, so
// the default plans a known false while the server stores true. Without the
// modifier that is an inconsistent-result-after-apply, then a permanent diff.
func TestVaultSourcedPlansSensitiveTrue(t *testing.T) {
	resp := run(t, str("vault"), str("env"), false, types.BoolNull())
	if !resp.PlanValue.ValueBool() {
		t.Fatalf("a vault-sourced variable must plan sensitive=true, got %v", resp.PlanValue)
	}
	if resp.Diagnostics.WarningsCount() != 0 {
		t.Errorf("config omitted sensitive, so there is nothing to warn about: %v", resp.Diagnostics)
	}
}

// The server forces sensitive for every credential category too, by the same
// mechanism and with the same consequence.
//
// DERIVED from the map rather than listing the categories, which is the
// difference between a test that tracks the rule and one that tracks whoever
// last edited it: this listed the two git categories by name, so #1933's two
// ansible categories were added to the map with the test still asserting only
// the pair it already knew. A hand-kept list here cannot fail for the category
// most likely to be wrong, which is always the newest one.
//
// The floor is what stops it passing on nothing: an emptied or renamed map
// would otherwise satisfy a loop over no entries.
func TestEveryCredentialCategoryPlansSensitiveTrue(t *testing.T) {
	if len(credentialCategories) < 4 {
		t.Fatalf("expected at least the four known credential categories, got %d: %v",
			len(credentialCategories), credentialCategories)
	}
	for cat := range credentialCategories {
		resp := run(t, str("static"), str(cat), false, types.BoolNull())
		if !resp.PlanValue.ValueBool() {
			t.Errorf("category %q must plan sensitive=true, got %v", cat, resp.PlanValue)
		}
		if resp.Diagnostics.HasError() {
			t.Errorf("category %q: %v", cat, resp.Diagnostics)
		}
	}
}

// The server's list is the authority, so a category here that it does not force
// would make the provider plan a secret the server leaves alone -- the mismatch
// in the other direction. Pinned as a literal set because the Python side is not
// importable here; the comment on `credentialCategories` names the constant it
// mirrors so a reader knows where to look.
func TestCredentialCategoriesMatchesTheServersSet(t *testing.T) {
	want := map[string]bool{
		"git_http_auth":        true,
		"git_ssh_auth":         true,
		"ansible_machine_auth": true,
		"ansible_vault_auth":   true,
	}
	for cat := range want {
		if !credentialCategories[cat] {
			t.Errorf("%q is forced sensitive by the server and missing here", cat)
		}
	}
	for cat := range credentialCategories {
		if !want[cat] {
			t.Errorf("%q is forced sensitive here and not by the server", cat)
		}
	}
}

// The negative path: an ordinary variable keeps the concrete false the default
// produced. Planning "known after apply" here would be a readability regression
// on every variable in the estate.
func TestOrdinaryVariableIsLeftAlone(t *testing.T) {
	resp := run(t, str("static"), str("terraform"), false, types.BoolNull())
	if resp.PlanValue.IsUnknown() {
		t.Fatal("an ordinary variable must keep a concrete planned value, not become unknown")
	}
	if resp.PlanValue.ValueBool() {
		t.Errorf("an ordinary variable must not be forced sensitive, got %v", resp.PlanValue)
	}
}

// An explicit sensitive = true on a vault variable already agrees with the
// server, so it must pass through without a warning.
func TestExplicitTrueOnVaultIsNotWarnedAbout(t *testing.T) {
	resp := run(t, str("vault"), str("env"), true, types.BoolValue(true))
	if !resp.PlanValue.ValueBool() {
		t.Fatalf("want true, got %v", resp.PlanValue)
	}
	if resp.Diagnostics.WarningsCount() != 0 {
		t.Errorf("explicit true matches the server; no warning expected: %v", resp.Diagnostics)
	}
}

// An explicit sensitive = false on a vault variable cannot take effect. Force
// it, but say so rather than overriding the operator in silence.
func TestExplicitFalseOnVaultIsForcedAndWarned(t *testing.T) {
	resp := run(t, str("vault"), str("env"), false, types.BoolValue(false))
	if !resp.PlanValue.ValueBool() {
		t.Fatalf("want the forced true, got %v", resp.PlanValue)
	}
	if resp.Diagnostics.WarningsCount() != 1 {
		t.Errorf("want exactly one warning explaining the override, got %v", resp.Diagnostics)
	}
}

// Either input still computed upstream: guessing would reintroduce the very
// mismatch this modifier removes, so the planned value goes unknown and apply
// settles it.
func TestUnknownInputsPlanUnknown(t *testing.T) {
	unknown := tftypes.NewValue(tftypes.String, tftypes.UnknownValue)

	if got := run(t, unknown, str("env"), false, types.BoolNull()); !got.PlanValue.IsUnknown() {
		t.Errorf("unknown value_source must plan unknown, got %v", got.PlanValue)
	}
	if got := run(t, str("static"), unknown, false, types.BoolNull()); !got.PlanValue.IsUnknown() {
		t.Errorf("unknown category must plan unknown, got %v", got.PlanValue)
	}
}

// A destroy plan has a null raw object; reading attributes off it would error.
func TestDestroyPlanIsIgnored(t *testing.T) {
	req := planmodifier.BoolRequest{
		Path:        path.Root("sensitive"),
		Plan:        tfsdk.Plan{Schema: testSchema(), Raw: tftypes.NewValue(objectType(), nil)},
		ConfigValue: types.BoolNull(),
		PlanValue:   types.BoolValue(true),
	}
	resp := planmodifier.BoolResponse{PlanValue: req.PlanValue}
	SensitiveForSecretBearingVariable().PlanModifyBool(context.Background(), req, &resp)

	if resp.Diagnostics.HasError() {
		t.Fatalf("a destroy plan must not produce diagnostics: %v", resp.Diagnostics)
	}
	if !resp.PlanValue.ValueBool() {
		t.Errorf("the planned value must be left as-is on destroy, got %v", resp.PlanValue)
	}
}
