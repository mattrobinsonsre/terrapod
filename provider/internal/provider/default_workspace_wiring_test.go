package provider

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/resource"
)

// Every resource and data source that takes a workspace id defaults it from the
// provider's `workspace`, which a Terrapod run exports as TERRAPOD_WORKSPACE
// (#2036). The consistency is the feature: a practitioner should not have to
// remember which half of the surface needs the id written down.
//
// planmods.DefaultWorkspaceID has its own mutation-checked tests, but those pass
// the default straight INTO the helper -- so they say nothing about whether a
// resource actually calls it, or calls it with the right value. Dropping the
// argument at a call site leaves every one of them green. This is the gate that
// catches that, and it catches the case nobody is watching for: a NEW resource
// with a workspace_id and no default, which would be an asymmetry no test owns.
//
// The subject is DERIVED from the provider's own schemas rather than listed, so
// a resource added later is in scope without anyone remembering to add it here.

// wiredByHand is for a surface that carries a workspace_id it must NOT default.
// An entry needs a reason, because the whole point of deriving the subject is
// that it cannot be opted out of silently.
var wiredByHand = map[string]string{}

// sourceFileFor maps a type name onto the file that declares it.
// terrapod_run_task -> internal/resources/run_task/resource.go
func sourceFileFor(typeName string, isDataSource bool) string {
	dir := strings.TrimPrefix(typeName, providerTypeName+"_")
	if isDataSource {
		return filepath.Join("..", "datasources", dir, "data_source.go")
	}
	return filepath.Join("..", "resources", dir, "resource.go")
}

func TestEverySurfaceTakingAWorkspaceIDDefaultsIt(t *testing.T) {
	ctx := context.Background()
	p := &terrapodProvider{}

	type subject struct {
		typeName     string
		isDataSource bool
	}
	var subjects []subject

	for _, factory := range p.Resources(ctx) {
		r := factory()
		var md resource.MetadataResponse
		r.Metadata(ctx, resource.MetadataRequest{ProviderTypeName: providerTypeName}, &md)
		var sr resource.SchemaResponse
		r.Schema(ctx, resource.SchemaRequest{}, &sr)
		if a, ok := sr.Schema.Attributes["workspace_id"]; ok && a.IsOptional() {
			subjects = append(subjects, subject{md.TypeName, false})
		}
	}
	for _, factory := range p.DataSources(ctx) {
		d := factory()
		var md datasource.MetadataResponse
		d.Metadata(ctx, datasource.MetadataRequest{ProviderTypeName: providerTypeName}, &md)
		var sr datasource.SchemaResponse
		d.Schema(ctx, datasource.SchemaRequest{}, &sr)
		if a, ok := sr.Schema.Attributes["workspace_id"]; ok && a.IsOptional() {
			subjects = append(subjects, subject{md.TypeName, true})
		}
	}

	sort.Slice(subjects, func(i, j int) bool { return subjects[i].typeName < subjects[j].typeName })

	// A floor, so a renamed helper or a changed layout fails here instead of
	// matching nothing and passing. 14 when written: 11 resources (the 7 that
	// already took a workspace id, plus the 4 inventory ones) and 3 data
	// sources. Derived by the test itself, so re-count from its own log line
	// rather than from this comment if it ever trips.
	if len(subjects) < 14 {
		t.Fatalf("found only %d surfaces with an optional workspace_id; the derivation has broken", len(subjects))
	}

	for _, s := range subjects {
		if why, exempt := wiredByHand[s.typeName]; exempt {
			t.Logf("%s: exempt (%s)", s.typeName, why)
			continue
		}
		path := sourceFileFor(s.typeName, s.isDataSource)
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Errorf("%s: cannot read %s: %v", s.typeName, path, err)
			continue
		}
		src := string(raw)

		// A resource fills the plan; a data source has no plan to fill and
		// resolves in Read. Either is wiring; neither is optional.
		callsHelper := strings.Contains(src, "planmods.DefaultWorkspaceID(")
		readsDefault := strings.Contains(src, "d.defaultWorkspace")
		if !callsHelper && !readsDefault {
			t.Errorf("%s (%s) takes an optional workspace_id but never defaults it — "+
				"call planmods.DefaultWorkspaceID in ModifyPlan, or resolve "+
				"d.defaultWorkspace in Read for a data source", s.typeName, path)
			continue
		}

		// And it must come from the provider, not be invented locally. A
		// literal, or the zero value, would satisfy the call above while
		// defaulting nothing.
		if callsHelper && !strings.Contains(src, "r.client.DefaultWorkspace") &&
			!strings.Contains(src, "r.defaultWorkspace") {
			t.Errorf("%s (%s) calls DefaultWorkspaceID but not with the provider's "+
				"default (r.client.DefaultWorkspace or r.defaultWorkspace)", s.typeName, path)
		}
		if readsDefault && !strings.Contains(src, "c.DefaultWorkspace") {
			t.Errorf("%s (%s) reads d.defaultWorkspace but never assigns it from "+
				"c.DefaultWorkspace in Configure", s.typeName, path)
		}
	}

	names := make([]string, 0, len(subjects))
	for _, s := range subjects {
		names = append(names, s.typeName)
	}
	t.Logf("checked %d surfaces: %s", len(names), fmt.Sprint(strings.Join(names, ", ")))
}
