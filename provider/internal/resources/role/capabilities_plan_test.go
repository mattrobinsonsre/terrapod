package role

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/mattrobinsonsre/terrapod/provider/internal/planmods"
)

// #2502. Changing a level on a role that does not author `capabilities` had no
// effect: the set was Optional+Computed with `UseStateForUnknown`, so the plan
// carried the PRIOR DERIVED set, the update sent it, and the server -- which
// treats any `capabilities` in a request as authored -- stored it verbatim and
// ignored the levels. The apply then failed "Provider produced inconsistent
// result after apply" and blocked every later apply of the configuration.
//
// The behaviour of the replacement modifier is pinned in
// internal/planmods/unknown_set_when_source_changes_test.go. What these tests
// pin is the two things that are this resource's own: that the schema actually
// uses it, and that the update request a plan turns into omits what it must.

// The schema half. Asserted structurally rather than behaviourally because the
// regression is a ONE-LINE change -- putting `UseStateForUnknown` back, or
// adding it alongside -- and either would settle the unknown straight back to
// the stale value with no behavioural test in this package noticing.
func TestCapabilitiesDoesNotUseStateForUnknown(t *testing.T) {
	ctx := context.Background()
	var r roleResource
	resp := &resource.SchemaResponse{}
	r.Schema(ctx, resource.SchemaRequest{}, resp)

	attr, ok := resp.Schema.Attributes["capabilities"].(schema.SetAttribute)
	if !ok {
		t.Fatalf("capabilities is not a SetAttribute: %T", resp.Schema.Attributes["capabilities"])
	}
	if len(attr.PlanModifiers) != 1 {
		t.Fatalf("capabilities carries %d plan modifiers, want exactly 1 -- "+
			"`UseStateForUnknown` beside the source-aware one would settle the unknown "+
			"back to the stale set and undo #2502", len(attr.PlanModifiers))
	}
	// Compared against the real constructor's own description, so a rename
	// cannot make this pass by matching a string nobody produces any more.
	want := planmods.UnknownSetWhenSourceChanges().Description(ctx)
	if got := attr.PlanModifiers[0].Description(ctx); got != want {
		t.Errorf("capabilities' plan modifier is %q, want the source-aware one (%q)", got, want)
	}
}

// All four levels must be named as sources: a role can be authored by any of
// them, and a source the modifier does not know about is a level whose edit
// goes back to being silently dropped.
func TestEveryLevelIsASourceOfCapabilities(t *testing.T) {
	ctx := context.Background()
	var r roleResource
	resp := &resource.SchemaResponse{}
	r.Schema(ctx, resource.SchemaRequest{}, resp)

	// DERIVED from the schema: every Optional+Computed `*_permission`
	// attribute, rather than a list of four anyone has to keep in step.
	levels := []string{}
	for name, a := range resp.Schema.Attributes {
		if len(name) > 11 && name[len(name)-11:] == "_permission" && a.IsOptional() {
			levels = append(levels, name)
		}
	}
	if len(levels) < 4 {
		t.Fatalf("found %d level attributes (%v); expected at least 4. If they were "+
			"renamed, fix the shape this test looks for rather than lowering the floor",
			len(levels), levels)
	}

	attr := resp.Schema.Attributes["capabilities"].(schema.SetAttribute)
	// Read the sources off the modifier itself. Comparing `Description()`
	// instead would pass with sources missing: it is a constant and says
	// nothing about them.
	withSources, ok := attr.PlanModifiers[0].(interface{ Sources() []string })
	if !ok {
		t.Fatalf("capabilities' plan modifier does not report its sources (%T), so "+
			"nothing can check that every level is wired", attr.PlanModifiers[0])
	}
	wired := map[string]bool{}
	for _, s := range withSources.Sources() {
		wired[s] = true
	}
	for _, l := range levels {
		if !wired[l] {
			t.Errorf("%q is a level the server derives `capabilities` from, but it is not "+
				"a source of the plan modifier — an edit to it would plan the stale set "+
				"and be silently dropped by the server (#2502)", l)
		}
	}
}

// The update request a plan turns into. An unauthored level holds the server's
// own derived summary, and sending a summary back as an instruction is the same
// defect one field over -- it is also not always expressible, because a set
// matching no preset summarises to the literal "custom", which the server
// cannot expand and refuses as an invalid level.
func TestAnUnauthoredLevelIsNotSentOnUpdate(t *testing.T) {
	plan := &roleModel{
		WorkspacePermission: types.StringValue("write"),
		// Derived summaries that state holds and the plan carried forward.
		PoolPermission:     types.StringValue("custom"),
		RegistryPermission: types.StringValue("read"),
		CatalogPermission:  types.StringValue("none"),
		Capabilities:       types.SetUnknown(types.StringType),
	}
	cfg := &roleModel{
		// Only the workspace level is authored.
		WorkspacePermission: types.StringValue("write"),
		PoolPermission:      types.StringNull(),
		RegistryPermission:  types.StringNull(),
		CatalogPermission:   types.StringNull(),
		Capabilities:        types.SetNull(types.StringType),
	}

	req := buildUpdateRoleRequest(plan, cfg)

	if req.WorkspacePermission != "write" {
		t.Errorf("the authored level must be sent, got %q", req.WorkspacePermission)
	}
	for name, got := range map[string]string{
		"pool":     req.PoolPermission,
		"registry": req.RegistryPermission,
		"catalog":  req.CatalogPermission,
	} {
		if got != "" {
			t.Errorf("%s level was not authored, so it must be omitted; got %q", name, got)
		}
	}
	if req.Capabilities != nil {
		t.Errorf("an unknown capabilities set must not be sent, got %v", req.Capabilities)
	}
}

// Every authored level is sent, so a configuration that names all four still
// writes all four.
func TestEveryAuthoredLevelIsSentOnUpdate(t *testing.T) {
	m := &roleModel{
		WorkspacePermission: types.StringValue("admin"),
		PoolPermission:      types.StringValue("write"),
		RegistryPermission:  types.StringValue("admin"),
		CatalogPermission:   types.StringValue("use"),
		Capabilities:        types.SetUnknown(types.StringType),
	}
	req := buildUpdateRoleRequest(m, m)
	if req.WorkspacePermission != "admin" || req.PoolPermission != "write" ||
		req.RegistryPermission != "admin" || req.CatalogPermission != "use" {
		t.Errorf("all four authored levels must be sent, got %+v", req)
	}
}

// An authored set is still sent, and the levels beside it are the ones the
// configuration names -- which is what the server now refuses if they disagree
// with the set, rather than dropping them.
func TestAnAuthoredCapabilitySetIsStillSent(t *testing.T) {
	caps, diags := types.SetValueFrom(
		context.Background(), types.StringType, []string{"workspace:read", "run:plan"})
	if diags.HasError() {
		t.Fatalf("building the set: %v", diags)
	}
	plan := &roleModel{
		WorkspacePermission: types.StringValue("custom"),
		PoolPermission:      types.StringValue("read"),
		RegistryPermission:  types.StringValue("read"),
		CatalogPermission:   types.StringValue("none"),
		Capabilities:        caps,
	}
	cfg := &roleModel{
		WorkspacePermission: types.StringNull(),
		PoolPermission:      types.StringNull(),
		RegistryPermission:  types.StringNull(),
		CatalogPermission:   types.StringNull(),
		Capabilities:        caps,
	}
	req := buildUpdateRoleRequest(plan, cfg)
	if len(req.Capabilities) != 2 {
		t.Errorf("an authored set must be sent, got %v", req.Capabilities)
	}
	if req.WorkspacePermission != "" {
		t.Errorf("no level was authored, so none may be sent; got %q", req.WorkspacePermission)
	}
}
