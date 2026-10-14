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
//
// WHAT IT DELIBERATELY DOES NOT COVER, so nobody "fixes the inconsistency" later:
// an attribute that names a ROLE in a relationship between two workspaces rather
// than the resource's own workspace. terrapod_run_trigger.source_workspace_id is
// one (defaulting it to self declares a workspace that triggers itself), and
// terrapod_remote_state_consumer's producer_workspace_id/consumer_workspace_id are
// the sharper case: the resource IS the grant, so neither end is "mine", and a
// guess is wrong in a security-relevant direction -- defaulting the producer when
// the author meant the consumer grants read of this workspace's state instead of
// requesting read of another's. Both stay required on purpose.

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
	// sources. Now 13: terrapod_inventory_settings was one of those four and
	// is gone (#1929) -- its six fields are workspace columns, so the roots
	// are set on terrapod_workspace itself and there is no separate resource
	// to default a workspace id on. Derived by the test itself, so re-count
	// from its own log line rather than from this comment if it ever trips.
	if len(subjects) < 13 {
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

// Where a resource calls BOTH plan modifiers, DefaultWorkspaceID must come
// first. KeepComputedWhenUnchanged treats an unknown planned value as a change,
// so a workspace_id still unknown by the time it runs makes a no-change re-plan
// look like an update and the carried computed attributes go unknown for ever --
// the #1619 shape, which this project has already shipped once.
//
// planmods pins that the ORDER matters (swap the two calls and its test fails
// with that diagnostic). This pins that every call site actually has it, which
// no behavioural test can see: a positional defect needs a positional gate.
func TestTheWorkspaceDefaultRunsBeforeKeepComputed(t *testing.T) {
	roots := []string{
		filepath.Join("..", "resources"),
		filepath.Join("..", "datasources"),
	}
	checked := 0
	for _, root := range roots {
		err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
			if err != nil || d.IsDir() || !strings.HasSuffix(path, ".go") {
				return err
			}
			raw, rerr := os.ReadFile(path)
			if rerr != nil {
				return rerr
			}
			src := string(raw)
			iDefault := strings.Index(src, "planmods.DefaultWorkspaceID(")
			iKeep := strings.Index(src, "planmods.KeepComputedWhenUnchanged(")
			if iDefault < 0 || iKeep < 0 {
				return nil // not a both-helpers file
			}
			checked++
			if iDefault > iKeep {
				t.Errorf("%s calls KeepComputedWhenUnchanged before DefaultWorkspaceID; "+
					"a workspace_id still unknown at that point makes every no-change "+
					"re-plan show an update that never converges (#1619)", path)
			}
			return nil
		})
		if err != nil {
			t.Fatalf("walking %s: %v", root, err)
		}
	}
	// Five at the time of writing (terrapod_variable and the four inventory
	// resources that carry a workspace_id); four since terrapod_inventory_settings
	// was removed in #1929. A floor, so a renamed helper fails here rather than
	// matching nothing and passing.
	if checked < 4 {
		t.Fatalf("found only %d call sites using both modifiers; the derivation has broken", checked)
	}
	t.Logf("checked %d call sites that use both modifiers", checked)
}
