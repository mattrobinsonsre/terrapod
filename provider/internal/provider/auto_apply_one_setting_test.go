package provider

// One setting, not two (#2503).
//
// `auto_apply` was `auto_apply_mode`'s boolean projection, carried on the
// workspace resource, the workspace data source and the autodiscovery rule.
// Two attributes for one setting is what let a configuration state a
// contradiction -- an explicit `auto_apply = false` beside
// `auto_apply_mode = "always"` -- which the server resolved towards MORE
// automation than the configuration asked for and then failed on with
// "Provider produced inconsistent result after apply", after the workspace had
// already been changed. With one attribute the contradiction is unrepresentable
// rather than merely refused, so the assertions that used to prove the two
// could not drift apart are replaced rather than repaired.
//
// What these replace:
//
//   - TestAutoApplyHasNoSchemaDefault asserted the boolean carried no schema
//     Default (#1294), because a known planned `false` could not survive the
//     server's projection. There is no boolean to default.
//   - TestAutoApplyModeIsNotGatedOnTheBoolean asserted the mode was sent
//     independently of the boolean. There is no boolean to be gated behind.
//
// The remaining risk is the boolean coming BACK -- on a new surface, or
// restored on an old one by someone reading a stale example -- so the subject
// is DERIVED from the provider's own registry rather than from a list of the
// three surfaces that used to hold it.

import (
	"context"
	"sort"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	dschema "github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	rschema "github.com/hashicorp/terraform-plugin-framework/resource/schema"
)

// catalogInstanceAutoApply is the ONE `auto_apply` that is not this setting,
// and it is exempted by name rather than by shape because nothing in the schema
// distinguishes them: `terrapod_catalog_instance.auto_apply` says whether the
// provision/reconfigure run it queues applies without waiting for a human. It
// is a property of that operation, not a standing workspace setting, there is
// no mode it is a projection of, and the catalog API still takes it. A blanket
// "no attribute may be called auto_apply" would take it with the rest.
const catalogInstanceAutoApply = "terrapod_catalog_instance"

// surfaceAttrs walks every registered resource and data source and returns, per
// type name, its top-level attributes.
func surfaceAttrs(t *testing.T) (resources, dataSources map[string]map[string]introspectable) {
	t.Helper()
	ctx := context.Background()
	p := New("test")()

	resources = map[string]map[string]introspectable{}
	for _, factory := range p.Resources(ctx) {
		r := factory()
		var md resource.MetadataResponse
		r.Metadata(ctx, resource.MetadataRequest{ProviderTypeName: providerTypeName}, &md)
		var sr resource.SchemaResponse
		r.Schema(ctx, resource.SchemaRequest{}, &sr)
		if sr.Diagnostics.HasError() {
			t.Fatalf("resource %s schema error: %v", md.TypeName, sr.Diagnostics)
		}
		m := map[string]introspectable{}
		for name, a := range sr.Schema.Attributes {
			m[name] = a
		}
		resources[md.TypeName] = m
	}

	dataSources = map[string]map[string]introspectable{}
	for _, factory := range p.DataSources(ctx) {
		d := factory()
		var md datasource.MetadataResponse
		d.Metadata(ctx, datasource.MetadataRequest{ProviderTypeName: providerTypeName}, &md)
		var sr datasource.SchemaResponse
		d.Schema(ctx, datasource.SchemaRequest{}, &sr)
		if sr.Diagnostics.HasError() {
			t.Fatalf("data source %s schema error: %v", md.TypeName, sr.Diagnostics)
		}
		m := map[string]introspectable{}
		for name, a := range sr.Schema.Attributes {
			m[name] = a
		}
		dataSources[md.TypeName] = m
	}
	return resources, dataSources
}

// TestTheAutoApplyBooleanIsGone derives the subject from the registry, so a
// NEW surface that declares the boolean fails here rather than shipping a
// second way to say the same thing.
func TestTheAutoApplyBooleanIsGone(t *testing.T) {
	resources, dataSources := surfaceAttrs(t)

	// A floor, because a walk that finds nothing passes every assertion below
	// while checking nothing at all.
	if len(resources) < 20 || len(dataSources) < 5 {
		t.Fatalf("walked %d resources and %d data sources; the registry accessors have "+
			"probably changed rather than the provider having shrunk",
			len(resources), len(dataSources))
	}

	var found []string
	for name, attrs := range resources {
		if _, ok := attrs["auto_apply"]; ok {
			found = append(found, name)
		}
	}
	for name, attrs := range dataSources {
		if _, ok := attrs["auto_apply"]; ok {
			found = append(found, "data."+name)
		}
	}
	sort.Strings(found)

	want := []string{catalogInstanceAutoApply}
	if len(found) != len(want) || found[0] != want[0] {
		t.Errorf("surfaces declaring `auto_apply` = %v, want exactly %v.\n"+
			"`auto_apply` is `auto_apply_mode`'s projection and was dropped (#2503): two "+
			"attributes for one setting is what let a configuration contradict itself and "+
			"be resolved towards more automation than it asked for. The catalog instance's "+
			"own `auto_apply` is a different thing -- whether the run it queues applies "+
			"without a human -- and is the only exemption.", found, want)
	}
}

// TestAutoApplyModeIsTheSetting pins the attribute that replaced it on every
// surface that carries it, rather than naming the surfaces: a surface that
// drops the mode as well would otherwise pass the gate above by having no
// auto-apply setting at all.
func TestAutoApplyModeIsTheSetting(t *testing.T) {
	resources, dataSources := surfaceAttrs(t)

	var carriers []string
	for name, attrs := range resources {
		a, ok := attrs["auto_apply_mode"]
		if !ok {
			continue
		}
		carriers = append(carriers, name)

		if !a.IsOptional() || !a.IsComputed() {
			t.Errorf("%s.auto_apply_mode must stay Optional+Computed (optional=%v computed=%v) "+
				"-- Computed is what lets the server's value land, and Optional is what lets a "+
				"configuration set it", name, a.IsOptional(), a.IsComputed())
		}
		str, ok := a.(rschema.StringAttribute)
		if !ok {
			t.Fatalf("%s.auto_apply_mode is %T, expected schema.StringAttribute", name, a)
		}
		// No Default, and the reason is drift rather than the API's old
		// either/or: Optional+Computed with a default makes a configuration
		// that never mentions the attribute plan the default against whatever
		// the workspace actually has, so every unmentioning config would
		// quietly push the setting back to `never`.
		if str.Default != nil {
			t.Errorf("%s.auto_apply_mode must NOT carry a schema default -- with "+
				"Optional+Computed, a default makes a configuration that does not set it "+
				"drift the server's value back to the default on every plan", name)
		}
	}

	for name, attrs := range dataSources {
		a, ok := attrs["auto_apply_mode"]
		if !ok {
			continue
		}
		carriers = append(carriers, "data."+name)
		if _, ok := a.(dschema.StringAttribute); !ok {
			t.Errorf("data.%s.auto_apply_mode is %T, expected schema.StringAttribute", name, a)
		}
		if !a.IsComputed() {
			t.Errorf("data.%s.auto_apply_mode must be Computed", name)
		}
	}

	sort.Strings(carriers)
	want := []string{
		"data.terrapod_workspace",
		"terrapod_autodiscovery_rule",
		"terrapod_workspace",
	}
	if len(carriers) != len(want) {
		t.Fatalf("surfaces carrying `auto_apply_mode` = %v, want %v -- a surface that lost "+
			"the mode as well would pass the boolean gate by having no auto-apply setting",
			carriers, want)
	}
	for i := range want {
		if carriers[i] != want[i] {
			t.Errorf("surfaces carrying `auto_apply_mode` = %v, want %v", carriers, want)
			break
		}
	}
}
