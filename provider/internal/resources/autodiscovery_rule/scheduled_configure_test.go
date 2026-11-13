package autodiscovery_rule

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// Scheduled configure (#2538), templated onto every workspace this rule
// materialises.
//
// The server has accepted and serialised all three attributes on
// POST/PATCH /api/v1/autodiscovery-rules since #2538, the columns exist on the
// rule, and the materialiser copies each one onto the workspaces the rule
// creates -- but the resource carried none of them, so a Terraform-declared
// rule could not template a schedule at all. A practitioner writing
// `scheduled_configure_cron` in a rule block got an unknown-attribute error,
// and the only way to set it was to edit each materialised workspace by hand,
// which the next materialisation does not repeat.
//
// `scheduled_configure_last_at` is deliberately NOT here: it is runtime state
// a workspace accrues from its own schedule, so the rule has no such column
// and there is nothing to template.

func TestTheScheduledConfigureAttributesAreAllOnTheSchema(t *testing.T) {
	var resp resource.SchemaResponse
	NewResource().Schema(context.Background(), resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema build error: %v", resp.Diagnostics)
	}

	for _, name := range []string{
		"scheduled_configure_enabled",
		"scheduled_configure_cron",
		"scheduled_configure_timezone",
	} {
		a, ok := resp.Schema.Attributes[name]
		if !ok {
			t.Errorf("%s is missing from the rule schema — a rule cannot template a "+
				"configure schedule onto the workspaces it creates", name)
			continue
		}
		if !a.IsOptional() {
			t.Errorf("%s must be Optional", name)
		}
		// Optional+Computed throughout, matching `terrapod_workspace`'s own
		// three and every templated setting beside them: the server answers
		// with a concrete value for each, so an Optional-only attribute plans
		// null against that answer and fails the apply of any rule that omits
		// it.
		if !a.IsComputed() {
			t.Errorf("%s must be Computed: the server supplies a default the provider "+
				"does not, so an Optional-only attribute plans null against a concrete "+
				"server answer and fails every apply that omits it", name)
		}
	}

	// The fourth name is a workspace-only computed field. Asserting its
	// absence is what stops it being added here by symmetry with the
	// workspace resource, where it would be permanently null.
	if _, ok := resp.Schema.Attributes["scheduled_configure_last_at"]; ok {
		t.Error("scheduled_configure_last_at is on the rule schema — it is runtime " +
			"state a workspace accrues, not something a rule templates, and the rule " +
			"has no such column for it to read")
	}
}

func TestTheScheduleReachesTheWireIncludingAnEmptyCronAndAFalse(t *testing.T) {
	// The empty cron is the case a truthiness guard would drop, and it is the
	// only way to REMOVE a schedule from a rule that has one: the server
	// membership-tests the key, so an absent key leaves the stored value
	// alone. `false` on the flag is the same shape.
	for _, tc := range []struct {
		name                   string
		enabled                types.Bool
		cron, timezone         types.String
		wantEnabled            any // nil means the key must be absent
		wantCron, wantTimezone any
	}{
		{
			name: "a full schedule", enabled: types.BoolValue(true),
			cron: types.StringValue("0 2 * * 2"), timezone: types.StringValue("Europe/London"),
			wantEnabled: true, wantCron: "0 2 * * 2", wantTimezone: "Europe/London",
		},
		{
			name: "an empty cron removes the schedule", enabled: types.BoolValue(true),
			cron: types.StringValue(""), timezone: types.StringValue("UTC"),
			wantEnabled: true, wantCron: "", wantTimezone: "UTC",
		},
		{
			name: "explicitly off", enabled: types.BoolValue(false),
			cron: types.StringNull(), timezone: types.StringNull(),
			wantEnabled: false, wantCron: nil, wantTimezone: nil,
		},
		{
			name: "omitted leaves the server's values alone", enabled: types.BoolNull(),
			cron: types.StringNull(), timezone: types.StringNull(),
			wantEnabled: nil, wantCron: nil, wantTimezone: nil,
		},
		{
			name: "unknown is not a declaration", enabled: types.BoolUnknown(),
			cron: types.StringUnknown(), timezone: types.StringUnknown(),
			wantEnabled: nil, wantCron: nil, wantTimezone: nil,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			attrs := buildAutodiscoveryRuleAttrs(&autodiscoveryRuleModel{
				ScheduledConfigureEnabled:  tc.enabled,
				ScheduledConfigureCron:     tc.cron,
				ScheduledConfigureTimezone: tc.timezone,
			})
			assertAttr(t, attrs, "scheduled-configure-enabled", tc.wantEnabled)
			assertAttr(t, attrs, "scheduled-configure-cron", tc.wantCron)
			assertAttr(t, attrs, "scheduled-configure-timezone", tc.wantTimezone)
		})
	}
}

func TestTheScheduleIsReadBackVerbatim(t *testing.T) {
	// Verbatim is the property that makes Optional+Computed safe here: an
	// attribute whose value the server rewrote could not read its own answer
	// back over a configured value without failing the apply with "Provider
	// produced inconsistent result after apply". The server validates the
	// cron and the zone and refuses what it cannot honour; it normalises
	// neither.
	ctx := context.Background()
	for _, tc := range []struct {
		name                   string
		attrs                  map[string]any
		wantEnabled            bool
		wantCron, wantTimezone string
	}{
		{
			name: "a schedule the rule set",
			attrs: map[string]any{
				"scheduled-configure-enabled":  true,
				"scheduled-configure-cron":     "0 2 * * 2",
				"scheduled-configure-timezone": "Europe/London",
			},
			wantEnabled: true, wantCron: "0 2 * * 2", wantTimezone: "Europe/London",
		},
		{
			name: "the server's defaults on a rule that never set one",
			attrs: map[string]any{
				"scheduled-configure-enabled":  false,
				"scheduled-configure-cron":     "",
				"scheduled-configure-timezone": "UTC",
			},
			wantEnabled: false, wantCron: "", wantTimezone: "UTC",
		},
		{
			name: "a cron with an equivalent spelling is not normalised",
			attrs: map[string]any{
				"scheduled-configure-enabled":  true,
				"scheduled-configure-cron":     "0 2 * * TUE",
				"scheduled-configure-timezone": "UTC",
			},
			wantEnabled: true, wantCron: "0 2 * * TUE", wantTimezone: "UTC",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			m := &autodiscoveryRuleModel{}
			if d := readAutodiscoveryRuleIntoModel(ctx, ruleResource(t, tc.attrs), m); d.HasError() {
				t.Fatalf("read: %v", d)
			}
			if m.ScheduledConfigureEnabled.ValueBool() != tc.wantEnabled {
				t.Errorf("scheduled_configure_enabled = %v, want %v",
					m.ScheduledConfigureEnabled, tc.wantEnabled)
			}
			if got := m.ScheduledConfigureCron.ValueString(); got != tc.wantCron {
				t.Errorf("scheduled_configure_cron = %q, want %q", got, tc.wantCron)
			}
			if got := m.ScheduledConfigureTimezone.ValueString(); got != tc.wantTimezone {
				t.Errorf("scheduled_configure_timezone = %q, want %q", got, tc.wantTimezone)
			}
		})
	}

	// None of the three is ever left null by a read: a null Computed
	// attribute against the server's concrete answer is the perpetual diff
	// the Optional+Computed shape exists to avoid. The server always sends
	// all three, so the interesting case is the one where it does not -- an
	// older server, or a trimmed response -- which must still produce a
	// concrete zero value rather than null.
	m := &autodiscoveryRuleModel{}
	if d := readAutodiscoveryRuleIntoModel(ctx, ruleResource(t, map[string]any{}), m); d.HasError() {
		t.Fatalf("read: %v", d)
	}
	for name, v := range map[string]bool{
		"scheduled_configure_enabled":  m.ScheduledConfigureEnabled.IsNull(),
		"scheduled_configure_cron":     m.ScheduledConfigureCron.IsNull(),
		"scheduled_configure_timezone": m.ScheduledConfigureTimezone.IsNull(),
	} {
		if v {
			t.Errorf("%s is null after a read, want a concrete zero value", name)
		}
	}
}
