package workspace

import (
	"context"
	"fmt"
	"testing"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/boolplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// Scheduled configure (#2538): a cron schedule as the third way a configure
// starts. Three settable attributes plus one read-only timestamp, and what
// these tests pin is that the provider carries all four faithfully.
//
// The hazard the two strings sit next to is the v1.9.0 advisory class: an
// Optional+Computed attribute whose value the SERVER rewrites cannot be read
// back, because the plan then disagrees with the result and the apply fails
// with "Provider produced inconsistent result after apply". The server stores
// both verbatim -- it validates the cron and refuses what it cannot honour,
// and normalises nothing -- which is what makes the modelling below safe, so
// `TestScheduledConfigureRoundTripsVerbatim` drives a server stand-in that
// echoes rather than one that reshapes.

// theScheduleServer stands in for the API: it echoes whatever reached the wire
// and applies the columns' own defaults -- off, no cron, UTC -- to whatever
// the request omitted.
func theScheduleServer(req terrapod.CreateWorkspaceRequest) *terrapod.Workspace {
	ws := &terrapod.Workspace{
		ID: "ws-a", Name: "a",
		ScheduledConfigureTimezone: "UTC",
	}
	if req.ScheduledConfigureEnabled != nil {
		ws.ScheduledConfigureEnabled = *req.ScheduledConfigureEnabled
	}
	// A plain string on the create request, so an omitted attribute and an
	// empty one arrive identically -- which is correct here, because empty IS
	// the default for both.
	if req.ScheduledConfigureCron != "" {
		ws.ScheduledConfigureCron = req.ScheduledConfigureCron
	}
	if req.ScheduledConfigureTimezone != "" {
		ws.ScheduledConfigureTimezone = req.ScheduledConfigureTimezone
	}
	return ws
}

func TestScheduledConfigureSettablesAreOptionalComputedWithUseStateForUnknown(t *testing.T) {
	var resp resource.SchemaResponse
	NewResource().Schema(context.Background(), resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema build error: %v", resp.Diagnostics)
	}

	// The modifier's IDENTITY rather than merely that there is one: a
	// different modifier would satisfy a presence check while leaving the
	// spurious-diff behaviour in place. Its concrete type is unexported, so
	// the expectation comes from the library rather than from a string.
	wantBool := fmt.Sprintf("%T", boolplanmodifier.UseStateForUnknown())
	wantString := fmt.Sprintf("%T", stringplanmodifier.UseStateForUnknown())

	for _, name := range []string{
		"scheduled_configure_enabled",
		"scheduled_configure_cron",
		"scheduled_configure_timezone",
	} {
		attr, ok := resp.Schema.Attributes[name]
		if !ok {
			t.Fatalf("attribute %q missing from the workspace schema", name)
		}
		if !attr.IsOptional() {
			t.Errorf("attribute %q must be Optional — it is the operator's setting", name)
		}
		if !attr.IsComputed() {
			t.Errorf("attribute %q must be Computed (#684): the server holds a value for it "+
				"even when the config omits it, so Optional-only plans NULL against the "+
				"server's answer and fails every apply that does not set it", name)
		}

		var mods []string
		var want string
		switch a := attr.(type) {
		case schema.BoolAttribute:
			want = wantBool
			for _, pm := range a.PlanModifiers {
				mods = append(mods, fmt.Sprintf("%T", pm))
			}
		case schema.StringAttribute:
			want = wantString
			for _, pm := range a.PlanModifiers {
				mods = append(mods, fmt.Sprintf("%T", pm))
			}
		default:
			t.Fatalf("attribute %q is %T, want a Bool or String attribute", name, attr)
		}
		found := false
		for _, got := range mods {
			if got == want {
				found = true
			}
		}
		if !found {
			t.Errorf("attribute %q has plan modifiers %v, want one of type %s like every "+
				"neighbour in the configure block — without it an Optional+Computed "+
				"attribute replans as unknown and shows a spurious diff on every plan",
				name, mods, want)
		}
	}
}

// The read-only half, and the assertion that matters is the NEGATIVE one: a
// timestamp the scheduler moves must not be Optional, or a configuration could
// claim to know when the schedule last fired -- and it must carry no
// UseStateForUnknown, because the value can legitimately change between plan
// and apply, which makes `(known after apply)` the honest plan output rather
// than diff noise to suppress.
func TestScheduledConfigureLastAtIsComputedOnlyAndServerVolatile(t *testing.T) {
	var resp resource.SchemaResponse
	NewResource().Schema(context.Background(), resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema build error: %v", resp.Diagnostics)
	}

	const name = "scheduled_configure_last_at"
	attr, ok := resp.Schema.Attributes[name]
	if !ok {
		t.Fatalf("attribute %q missing from the workspace schema", name)
	}
	if attr.IsOptional() || attr.IsRequired() {
		t.Errorf("attribute %q must be Computed-only: it is when the schedule last fired, "+
			"which is server state and is what the next occurrence is measured from", name)
	}
	if !attr.IsComputed() {
		t.Errorf("attribute %q must be Computed", name)
	}
	sa, ok := attr.(schema.StringAttribute)
	if !ok {
		t.Fatalf("attribute %q is %T, want schema.StringAttribute", name, attr)
	}
	if len(sa.PlanModifiers) != 0 {
		got := make([]string, 0, len(sa.PlanModifiers))
		for _, pm := range sa.PlanModifiers {
			got = append(got, fmt.Sprintf("%T", pm))
		}
		t.Errorf("attribute %q carries plan modifiers %v — it is server-volatile, so it must "+
			"carry none, exactly like drift_last_checked_at beside it", name, got)
	}
}

func TestScheduledConfigureRoundTripsVerbatim(t *testing.T) {
	ctx := context.Background()

	for _, tc := range []struct {
		name         string
		enabled      types.Bool
		cron         types.String
		timezone     types.String
		wantEnabled  bool
		wantCron     string
		wantTimezone string
	}{
		{
			"omitted takes the columns' defaults",
			types.BoolUnknown(), types.StringUnknown(), types.StringUnknown(),
			false, "", "UTC",
		},
		{
			"a weekly schedule in a named zone",
			types.BoolValue(true), types.StringValue("0 2 * * 2"), types.StringValue("Europe/London"),
			true, "0 2 * * 2", "Europe/London",
		},
		// The case this file exists for. The server stores the cron exactly as
		// written, so a spelling with a list rather than a step has to come
		// back as the configuration wrote it. Anything that canonicalises
		// either side fails HERE and nowhere else.
		{
			"an unusual spelling survives untouched",
			types.BoolValue(true), types.StringValue("0,30 1,13 * * 1-5"), types.StringValue("America/New_York"),
			true, "0,30 1,13 * * 1-5", "America/New_York",
		},
		// An empty cron is a REAL value -- it means no schedule -- so it must
		// read back as "" rather than as null.
		{
			"no schedule",
			types.BoolValue(false), types.StringValue(""), types.StringValue("UTC"),
			false, "", "UTC",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceModel
			m.ScheduledConfigureEnabled = tc.enabled
			m.ScheduledConfigureCron = tc.cron
			m.ScheduledConfigureTimezone = tc.timezone
			planned := m

			req, diags := buildCreateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("create request: %v", diags)
			}

			ws := theScheduleServer(req)

			// The read starts from the plan, as Create does.
			state := planned
			if diags := readWorkspaceIntoModel(ctx, ws, &state); diags.HasError() {
				t.Fatalf("read: %v", diags)
			}

			assertConsistentBool(t, tc.name+" scheduled_configure_enabled",
				planned.ScheduledConfigureEnabled, state.ScheduledConfigureEnabled)
			assertConsistent(t, tc.name+" scheduled_configure_cron",
				planned.ScheduledConfigureCron, state.ScheduledConfigureCron)
			assertConsistent(t, tc.name+" scheduled_configure_timezone",
				planned.ScheduledConfigureTimezone, state.ScheduledConfigureTimezone)

			for what, got := range map[string]interface{ IsNull() bool }{
				"scheduled_configure_enabled":  state.ScheduledConfigureEnabled,
				"scheduled_configure_cron":     state.ScheduledConfigureCron,
				"scheduled_configure_timezone": state.ScheduledConfigureTimezone,
			} {
				if got.IsNull() {
					t.Errorf("%s read back as null — a Computed attribute over a "+
						"non-nullable column must hold a concrete value after apply", what)
				}
			}
			if got := state.ScheduledConfigureEnabled.ValueBool(); got != tc.wantEnabled {
				t.Errorf("scheduled_configure_enabled = %v, want %v", got, tc.wantEnabled)
			}
			if got := state.ScheduledConfigureCron.ValueString(); got != tc.wantCron {
				t.Errorf("scheduled_configure_cron = %q, want %q", got, tc.wantCron)
			}
			if got := state.ScheduledConfigureTimezone.ValueString(); got != tc.wantTimezone {
				t.Errorf("scheduled_configure_timezone = %q, want %q", got, tc.wantTimezone)
			}
		})
	}
}

// The write legs, in both directions. The UPDATE request holds the two strings
// as pointers so that an explicit `scheduled_configure_cron = ""` REACHES the
// wire: that is how an operator removes a schedule, and a plain string would
// drop it, leaving the workspace configuring itself every week with the plan
// reporting success.
func TestScheduledConfigureTravelsOnBothWritePaths(t *testing.T) {
	ctx := context.Background()

	for _, tc := range []struct {
		name     string
		cron     types.String
		wantSent bool // does the cron reach the UPDATE body at all?
		wantCron string
	}{
		{"a schedule", types.StringValue("0 2 * * 2"), true, "0 2 * * 2"},
		{"removing the schedule", types.StringValue(""), true, ""},
		{"omitted", types.StringNull(), false, ""},
		{"unknown", types.StringUnknown(), false, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceModel
			m.ScheduledConfigureCron = tc.cron

			update, diags := buildUpdateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("update: %v", diags)
			}
			got := update.ScheduledConfigureCron
			if !tc.wantSent {
				if got != nil {
					t.Errorf("update scheduled-configure-cron sent %q when the "+
						"configuration said nothing", *got)
				}
				return
			}
			if got == nil {
				t.Fatalf("update scheduled-configure-cron was dropped, so %q could never "+
					"reach the server — an empty cron is how the schedule is removed", tc.wantCron)
			}
			if *got != tc.wantCron {
				t.Errorf("update scheduled-configure-cron = %q, want %q", *got, tc.wantCron)
			}
		})
	}

	// The flag, on both legs, as a pointer for the reason its neighbours are.
	for _, tc := range []struct {
		name string
		val  types.Bool
		want *bool
	}{
		{"on", types.BoolValue(true), ptr(true)},
		{"off", types.BoolValue(false), ptr(false)},
		{"omitted", types.BoolNull(), nil},
		{"unknown", types.BoolUnknown(), nil},
	} {
		t.Run("enabled "+tc.name, func(t *testing.T) {
			var m workspaceModel
			m.ScheduledConfigureEnabled = tc.val

			create, diags := buildCreateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("create: %v", diags)
			}
			checkOpBoolPtr(t, "create scheduled-configure-enabled",
				create.ScheduledConfigureEnabled, tc.want)

			update, diags := buildUpdateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("update: %v", diags)
			}
			checkOpBoolPtr(t, "update scheduled-configure-enabled",
				update.ScheduledConfigureEnabled, tc.want)
		})
	}
}

// The read-only timestamp's own round trip. Null when it has never fired is
// right here and only here: nothing in a configuration can disagree with a
// Computed-only attribute, so there is no inconsistent-result risk to trade
// against saying honestly that there is no value yet.
func TestScheduledConfigureLastAtReadsBack(t *testing.T) {
	ctx := context.Background()

	for _, tc := range []struct {
		name     string
		server   string
		wantNull bool
	}{
		{"never fired", "", true},
		{"fired", "2026-11-10T02:00:00Z", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceModel
			ws := &terrapod.Workspace{
				ID: "ws-a", Name: "a",
				ScheduledConfigureLastAt: tc.server,
			}
			if diags := readWorkspaceIntoModel(ctx, ws, &m); diags.HasError() {
				t.Fatalf("read: %v", diags)
			}
			got := m.ScheduledConfigureLastAt
			if tc.wantNull {
				if !got.IsNull() {
					t.Errorf("scheduled_configure_last_at = %v, want null when the "+
						"schedule has never fired", got)
				}
				return
			}
			if got.IsNull() || got.IsUnknown() {
				t.Fatalf("scheduled_configure_last_at read back as %v, want a concrete value", got)
			}
			if got.ValueString() != tc.server {
				t.Errorf("scheduled_configure_last_at = %q, want %q", got.ValueString(), tc.server)
			}
		})
	}
}
