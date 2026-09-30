package provider

// Provider schema contract gate (#550).
//
// terraform-provider-terrapod is a public consumer surface: operators' HCL and
// state depend on every resource/data-source attribute — its name, its type, and
// whether it's required/optional/computed/sensitive. Removing or renaming an
// attribute, or changing its type or requiredness, is a BREAKING change that can
// force a resource replacement or fail plans/applies against existing state.
//
// This test instantiates every registered resource and data source, walks its
// schema attributes, and freezes them against a committed golden (schema.golden).
// A removal/rename/retype fails the build as breaking (MAJOR bump or a documented
// deprecation window, not a regen); adding an attribute is additive — regenerate:
//
//	UPDATE_SCHEMA=1 go test ./internal/provider/ -run TestProviderSchemaContract
//
// The attribute type is rendered via attr.Type.String(), which for nested object
// / list attributes includes the full nested shape — so a nested rename/retype is
// caught too.

import (
	"context"
	"fmt"
	"os"
	"sort"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/datasource"
	dschema "github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	rschema "github.com/hashicorp/terraform-plugin-framework/resource/schema"
)

const schemaGolden = "schema.golden"

const providerTypeName = "terrapod"

// introspectable is the subset of the framework attribute interface both
// resource and data-source attributes satisfy.
type introspectable interface {
	GetType() attr.Type
	IsRequired() bool
	IsOptional() bool
	IsComputed() bool
	IsSensitive() bool
}

func collectAttrs[A introspectable](typeName string, attrs map[string]A) []string {
	out := make([]string, 0, len(attrs))
	for name, a := range attrs {
		out = append(out, fmt.Sprintf(
			"%s.%s required=%t optional=%t computed=%t sensitive=%t type=%s",
			typeName, name,
			a.IsRequired(), a.IsOptional(), a.IsComputed(), a.IsSensitive(),
			a.GetType().String(),
		))
	}
	return out
}

func providerSchemaSurface(t *testing.T) []string {
	t.Helper()
	ctx := context.Background()
	p := New("test")()

	var out []string

	for _, factory := range p.Resources(ctx) {
		r := factory()
		var md resource.MetadataResponse
		r.Metadata(ctx, resource.MetadataRequest{ProviderTypeName: providerTypeName}, &md)
		var sr resource.SchemaResponse
		r.Schema(ctx, resource.SchemaRequest{}, &sr)
		if sr.Diagnostics.HasError() {
			t.Fatalf("resource %s schema error: %v", md.TypeName, sr.Diagnostics)
		}
		out = append(out, collectAttrs("resource "+md.TypeName, sr.Schema.Attributes)...)
		if len(sr.Schema.Blocks) > 0 {
			t.Fatalf("resource %s uses schema Blocks, which this gate does not yet freeze — extend the gate", md.TypeName)
		}
	}

	for _, factory := range p.DataSources(ctx) {
		d := factory()
		var md datasource.MetadataResponse
		d.Metadata(ctx, datasource.MetadataRequest{ProviderTypeName: providerTypeName}, &md)
		var sr datasource.SchemaResponse
		d.Schema(ctx, datasource.SchemaRequest{}, &sr)
		if sr.Diagnostics.HasError() {
			t.Fatalf("data source %s schema error: %v", md.TypeName, sr.Diagnostics)
		}
		out = append(out, collectAttrs("data "+md.TypeName, sr.Schema.Attributes)...)
		if len(sr.Schema.Blocks) > 0 {
			t.Fatalf("data source %s uses schema Blocks, which this gate does not yet freeze — extend the gate", md.TypeName)
		}
	}

	sort.Strings(out)
	return out
}

// Compile-time assertions that the framework attribute interfaces satisfy
// introspectable (so the generic collectAttrs is well-typed).
var (
	_ introspectable = rschema.StringAttribute{}
	_ introspectable = dschema.StringAttribute{}
)

func TestProviderSchemaContract(t *testing.T) {
	current := providerSchemaSurface(t)

	if os.Getenv("UPDATE_SCHEMA") != "" {
		if err := os.WriteFile(schemaGolden, []byte(strings.Join(current, "\n")+"\n"), 0o644); err != nil {
			t.Fatalf("write golden: %v", err)
		}
		return
	}

	raw, err := os.ReadFile(schemaGolden)
	if err != nil {
		t.Fatalf("read %s (generate with UPDATE_SCHEMA=1 go test ./internal/provider/ -run TestProviderSchemaContract): %v", schemaGolden, err)
	}
	golden := map[string]bool{}
	for line := range strings.SplitSeq(strings.TrimSpace(string(raw)), "\n") {
		if line != "" {
			golden[line] = true
		}
	}
	cur := map[string]bool{}
	for _, line := range current {
		cur[line] = true
	}

	var removed, added []string
	for line := range golden {
		if !cur[line] {
			removed = append(removed, line)
		}
	}
	for line := range cur {
		if !golden[line] {
			added = append(added, line)
		}
	}

	// Separate the attributes that are present on BOTH sides with a different
	// rendering from the ones that genuinely appeared or vanished (#1911).
	//
	// The golden is a line set, and a line carries an attribute's whole rendered
	// type — including, for a nested object, every field inside it. So ADDING a
	// field to a nested object diffs as one removal plus one addition, and the
	// gate announced a purely additive change under a BREAKING banner. That is
	// worse than a false alarm: it is a false alarm on the one banner that must
	// never be regenerated past, and a reviewer who sees it a few times learns to
	// regenerate past all of them.
	//
	// This changes no verdict — a changed attribute still fails the build, which
	// is right, because widening a nested object and retyping it are both changes
	// and only a person can tell which. It changes what the failure is CALLED, so
	// the reviewer is asked the question they can actually answer.
	byKey := func(line string) string {
		if i := strings.Index(line, " required="); i >= 0 {
			return line[:i]
		}
		return line
	}
	goneKeys := map[string]string{}
	for _, line := range removed {
		goneKeys[byKey(line)] = line
	}
	var changed, reallyRemoved, reallyAdded []string
	for _, line := range added {
		if before, both := goneKeys[byKey(line)]; both {
			changed = append(changed, fmt.Sprintf("was: %s\n  now: %s", before, line))
			continue
		}
		reallyAdded = append(reallyAdded, line)
	}
	stillChanged := map[string]bool{}
	for _, c := range changed {
		stillChanged[byKey(strings.TrimPrefix(strings.SplitN(c, "\n", 2)[0], "was: "))] = true
	}
	for _, line := range removed {
		if !stillChanged[byKey(line)] {
			reallyRemoved = append(reallyRemoved, line)
		}
	}
	sort.Strings(reallyRemoved)
	sort.Strings(reallyAdded)
	sort.Strings(changed)

	if len(reallyRemoved) > 0 {
		t.Errorf("BREAKING: provider schema attributes removed/renamed (operators' HCL + state depend on these — MAJOR bump or a documented deprecation window, NOT a golden regen):\n  %s", strings.Join(reallyRemoved, "\n  "))
	}
	if len(changed) > 0 {
		t.Errorf("Provider schema attributes CHANGED — the attribute still exists and its rendering moved. Decide which this is before regenerating:\n"+
			"  * a nested object that GAINED a field, or a requiredness relaxation → additive, regenerate\n"+
			"  * a type change, a narrowing, or a field REMOVED from a nested object → BREAKING, do not regenerate\n"+
			"  UPDATE_SCHEMA=1 go test ./internal/provider/ -run TestProviderSchemaContract\n  %s", strings.Join(changed, "\n  "))
	}
	if len(reallyAdded) > 0 {
		t.Errorf("Provider schema attributes added (additive). Regenerate the golden:\n  UPDATE_SCHEMA=1 go test ./internal/provider/ -run TestProviderSchemaContract\n  added:\n  %s", strings.Join(reallyAdded, "\n  "))
	}
}
