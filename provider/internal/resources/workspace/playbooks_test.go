package workspace

import (
	"context"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-go/tftypes"
	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// The playbook allow-list (#2527) and the plan-time check on its pairing with
// `default_playbook`.
//
// The attribute itself is an ordinary Optional+Computed list and follows
// `inventory_paths` exactly; what is new here is the VALIDATOR, which is the
// point of the issue. The server refuses the same two contradictions with a
// 422, so nothing below is the only thing standing between a practitioner and
// a bad workspace -- it is what turns an apply-time failure into an error they
// see before Terraform has decided anything.

// ── The validator, driven through ValidateConfig ────────────────────────────

func TestThePlaybookPairIsCheckedAtPlanTime(t *testing.T) {
	// Driven through ValidateConfig itself, not through
	// validatePlaybookAllowList: a test that hands the helper its arguments
	// proves the helper formats a diagnostic and says nothing about whether
	// anything calls it. Deleting the call from ValidateConfig must fail this.
	ctx := context.Background()

	strs := func(vals ...tftypes.Value) tftypes.Value {
		return tftypes.NewValue(tftypes.List{ElementType: tftypes.String}, vals)
	}
	str := func(s string) tftypes.Value { return tftypes.NewValue(tftypes.String, s) }
	nullStr := tftypes.NewValue(tftypes.String, nil)

	for _, tc := range []struct {
		name      string
		playbooks tftypes.Value
		def       tftypes.Value
		wantError bool
		// wantIn are substrings the refusal must carry, so the message names
		// the offending value and the values that would be accepted. Without
		// them a practitioner has to read the docs to act on the error.
		wantIn []string
	}{
		{
			name:      "a consistent pair is accepted",
			playbooks: strs(str("release.yml"), str("restart.yml")),
			def:       str("release.yml"),
		},
		{
			name: "no playbooks at all is accepted — the attribute is opt-in",
			// Every workspace that existed before this change looks like this,
			// so refusing here would break the whole deployment at upgrade.
			playbooks: tftypes.NewValue(tftypes.List{ElementType: tftypes.String}, nil),
			def:       str("release.yml"),
		},
		{
			name:      "an empty list is the open state and is accepted",
			playbooks: strs(),
			def:       nullStr,
		},
		{
			name:      "a default outside the list is refused",
			playbooks: strs(str("release.yml"), str("restart.yml")),
			def:       str("relase.yml"), // the typo this exists to catch
			wantError: true,
			wantIn:    []string{`"relase.yml"`, `"release.yml"`, `"restart.yml"`},
		},
		{
			name:      "a list with no default is refused",
			playbooks: strs(str("release.yml"), str("restart.yml")),
			def:       nullStr,
			wantError: true,
			wantIn:    []string{`"release.yml"`, `"restart.yml"`, "default_playbook"},
		},
		{
			name: "a list with an EMPTY default is refused",
			// "" is how an operator says "no default" on this attribute, so it
			// is the same contradiction written the other way.
			playbooks: strs(str("release.yml")),
			def:       str(""),
			wantError: true,
			wantIn:    []string{`"release.yml"`},
		},
		{
			name: "an unknown list is left to the API",
			// Comes from an interpolation that has not resolved yet. The pair
			// is not decidable here, and the 422 is the backstop.
			playbooks: tftypes.NewValue(tftypes.List{ElementType: tftypes.String}, tftypes.UnknownValue),
			def:       str("release.yml"),
		},
		{
			name: "one unknown ELEMENT is enough to skip the check",
			// The unknown entry might resolve to exactly the default, so
			// refusing would fail a plan that is going to be perfectly valid.
			// This is the case a list-level IsUnknown() test alone would miss.
			playbooks: strs(str("restart.yml"), tftypes.NewValue(tftypes.String, tftypes.UnknownValue)),
			def:       str("release.yml"),
		},
		{
			name:      "an unknown default is left to the API",
			playbooks: strs(str("release.yml")),
			def:       tftypes.NewValue(tftypes.String, tftypes.UnknownValue),
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := NewResource()
			cfg := configWith(t, ctx, r, map[string]tftypes.Value{
				"playbooks":        tc.playbooks,
				"default_playbook": tc.def,
			})

			var resp resource.ValidateConfigResponse
			r.(interface {
				ValidateConfig(context.Context, resource.ValidateConfigRequest, *resource.ValidateConfigResponse)
			}).ValidateConfig(ctx, resource.ValidateConfigRequest{Config: cfg}, &resp)

			if got := resp.Diagnostics.HasError(); got != tc.wantError {
				t.Fatalf("ValidateConfig error = %v, want %v (diags: %v)",
					got, tc.wantError, resp.Diagnostics)
			}
			if !tc.wantError {
				return
			}
			var detail string
			for _, d := range resp.Diagnostics.Errors() {
				detail += d.Summary() + " " + d.Detail()
			}
			for _, want := range tc.wantIn {
				if !strings.Contains(detail, want) {
					t.Errorf("the refusal does not mention %s, so a practitioner "+
						"cannot act on it without reading the docs: %q", want, detail)
				}
			}
		})
	}
}

// ── The attribute, which follows inventory_paths ────────────────────────────

func TestPlaybooksTravelVerbatimAndAnEmptyListClears(t *testing.T) {
	// Stored byte for byte: no trim, no sort, no de-duplication. The server
	// returns the list it was sent, so anything the provider normalised on the
	// way out would come back different from what was planned and abort the
	// apply with "Provider produced inconsistent result after apply". The
	// server refuses a duplicate with a 422 for the same reason, rather than
	// collapsing it.
	ctx := context.Background()

	var m workspaceModel
	m.Playbooks = types.ListValueMust(types.StringType, []attr.Value{
		types.StringValue("restart.yml"),
		types.StringValue("Release.yml"),
		types.StringValue(" spaced.yml"),
	})

	create, diags := buildCreateWorkspaceRequest(ctx, &m)
	if diags.HasError() {
		t.Fatalf("create: %v", diags)
	}
	want := []string{"restart.yml", "Release.yml", " spaced.yml"}
	assertPlaybooks(t, "create", create.Playbooks, want)

	update, diags := buildUpdateWorkspaceRequest(ctx, &m)
	if diags.HasError() {
		t.Fatalf("update: %v", diags)
	}
	assertPlaybooks(t, "update", update.Playbooks, want)

	// An explicit empty list is a real request: it restores the open state in
	// which any playbook may run, and it is the only way back out of an
	// allow-list. A nil slice is dropped by `omitempty`, so the clear has to
	// reach the wire as a non-nil empty slice.
	var cleared workspaceModel
	cleared.Playbooks = types.ListValueMust(types.StringType, []attr.Value{})
	createCleared, diags := buildCreateWorkspaceRequest(ctx, &cleared)
	if diags.HasError() {
		t.Fatalf("create: %v", diags)
	}
	if createCleared.Playbooks == nil {
		t.Error("create dropped an explicit empty playbooks — the one config that " +
			"reopens the workspace to any playbook would silently do nothing")
	} else if len(createCleared.Playbooks) != 0 {
		t.Errorf("create sent playbooks %v, want empty", createCleared.Playbooks)
	}
	updateCleared, diags := buildUpdateWorkspaceRequest(ctx, &cleared)
	if diags.HasError() {
		t.Fatalf("update: %v", diags)
	}
	if updateCleared.Playbooks == nil {
		t.Error("update dropped an explicit empty playbooks")
	}

	// Omitted means omitted: the allow-list may have been set in the UI or
	// through the bulk-update endpoint, and clobbering it on every apply of a
	// config that does not manage it would be worse than the diff it hides.
	var omitted workspaceModel
	omitted.Playbooks = types.ListNull(types.StringType)
	updateOmitted, diags := buildUpdateWorkspaceRequest(ctx, &omitted)
	if diags.HasError() {
		t.Fatalf("update: %v", diags)
	}
	if updateOmitted.Playbooks != nil {
		t.Errorf("update sent playbooks %v when the config omitted it", updateOmitted.Playbooks)
	}
}

func TestPlaybooksReadBackPreservesAPriorNull(t *testing.T) {
	// The null-versus-empty rule every list on this resource follows: a prior
	// null plus an empty server answer stays null, so a config that never
	// declared the attribute does not plan a diff against `[]` on every run.
	ctx := context.Background()

	var m workspaceModel
	m.Playbooks = types.ListNull(types.StringType)
	if diags := readWorkspaceIntoModel(ctx, &terrapod.Workspace{ID: "ws-a", Name: "a"}, &m); diags.HasError() {
		t.Fatalf("read: %v", diags)
	}
	if !m.Playbooks.IsNull() {
		t.Errorf("playbooks materialised as %v from a prior null and an empty server value",
			m.Playbooks)
	}

	// With values present it reads them back exactly, order included.
	var m2 workspaceModel
	m2.Playbooks = types.ListNull(types.StringType)
	ws := &terrapod.Workspace{
		ID: "ws-a", Name: "a",
		Playbooks: []string{"restart.yml", "release.yml"},
	}
	if diags := readWorkspaceIntoModel(ctx, ws, &m2); diags.HasError() {
		t.Fatalf("read: %v", diags)
	}
	els := m2.Playbooks.Elements()
	if len(els) != 2 ||
		els[0].(types.String).ValueString() != "restart.yml" ||
		els[1].(types.String).ValueString() != "release.yml" {
		t.Errorf("playbooks read back as %v — a re-order is an inconsistent result "+
			"after apply however harmless it would be at run time", els)
	}
}

func assertPlaybooks(t *testing.T, what string, got, want []string) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("%s sent playbooks %v, want %v", what, got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("%s sent playbooks[%d] = %q, want %q — the list is stored byte "+
				"for byte, so nothing may be trimmed, lower-cased or sorted on the way out",
				what, i, got[i], want[i])
		}
	}
}
