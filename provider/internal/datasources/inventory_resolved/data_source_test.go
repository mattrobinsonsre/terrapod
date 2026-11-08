package inventory_resolved

import (
	"context"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/tfsdk"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-go/tftypes"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// ── Schema ───────────────────────────────────────────────────────────────────

func TestSchemaShape(t *testing.T) {
	ctx := context.Background()
	var resp datasource.SchemaResponse
	NewDataSource().Schema(ctx, datasource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema: %v", resp.Diagnostics)
	}

	attrs := resp.Schema.Attributes
	for _, name := range []string{
		"workspace_id", "limit", "provenance", "hosts", "var_provenance",
		"groups", "group_children", "host_count", "group_count",
	} {
		if _, ok := attrs[name]; !ok {
			t.Errorf("attribute %q is missing", name)
		}
	}
	if len(attrs) != 9 {
		t.Errorf("unexpected attribute count %d: %v", len(attrs), attrs)
	}

	// Optional+Computed since #2036: it defaults to the provider's `workspace`,
	// which a run exports. Required would make `provider "terrapod" {}` unusable
	// inside a run, which is the whole point of the default.
	if attrs["workspace_id"].IsRequired() || !attrs["workspace_id"].IsOptional() ||
		!attrs["workspace_id"].IsComputed() {
		t.Error("workspace_id must be Optional+Computed so it can default (#2036)")
	}
	if !attrs["limit"].IsOptional() || attrs["limit"].IsRequired() {
		t.Error("limit is optional")
	}
	// Optional-only, deliberately NOT Computed: the practitioner is the only
	// source of it, and a Computed input would invite writing a resolved value
	// back over a null config -- the inconsistent-result shape.
	if !attrs["provenance"].IsOptional() || attrs["provenance"].IsRequired() ||
		attrs["provenance"].IsComputed() {
		t.Error("provenance must be optional-only: nothing else supplies it")
	}
	for _, name := range []string{"hosts", "var_provenance", "groups", "group_children", "host_count", "group_count"} {
		if !attrs[name].IsComputed() || attrs[name].IsOptional() || attrs[name].IsRequired() {
			t.Errorf("%s must be computed-only", name)
		}
	}
	if got := attrs["hosts"].GetType().String(); got != "types.MapType[types.MapType[basetypes.StringType]]" {
		t.Errorf("hosts type = %s", got)
	}
	for _, name := range []string{"groups", "group_children"} {
		if got := attrs[name].GetType().String(); got != "types.MapType[types.ListType[basetypes.StringType]]" {
			t.Errorf("%s type = %s", name, got)
		}
	}
}

// A practitioner reading an empty `groups[name]` as "targets nothing" is the
// mistake this data source can cause, so the description has to say that
// ansible does not flatten nesting into a group's own host list.
func TestTheGroupsDescriptionWarnsThatNestingIsNotFlattened(t *testing.T) {
	ctx := context.Background()
	var resp datasource.SchemaResponse
	NewDataSource().Schema(ctx, datasource.SchemaRequest{}, &resp)

	desc := resp.Schema.Attributes["groups"].GetDescription()
	for _, want := range []string{"DIRECT", "group_children", "targets nothing"} {
		if !strings.Contains(desc, want) {
			t.Errorf("the groups description must mention %q: %q", want, desc)
		}
	}
}

// ── The projection ───────────────────────────────────────────────────────────

// A Terraform map is homogeneous and an inventory variable is not, so a string
// is given verbatim and anything else is JSON-encoded.
func TestRenderValueGivesAStringVerbatimAndEncodesTheRest(t *testing.T) {
	for _, tc := range []struct {
		name string
		in   any
		want string
	}{
		{"string is verbatim, not re-quoted", "deploy", "deploy"},
		{"a string that looks like json is still verbatim", `{"a":1}`, `{"a":1}`},
		{"number", float64(2222), "2222"},
		{"bool", true, "true"},
		{"list", []any{"a", "b"}, `["a","b"]`},
		{"object", map[string]any{"k": "v"}, `{"k":"v"}`},
		{"null is empty, because a map cannot hold a null element", nil, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := renderValue(tc.in); got != tc.want {
				t.Errorf("renderValue(%#v) = %q, want %q", tc.in, got, tc.want)
			}
		})
	}
}

// Exhaustive, deliberately unlike ansible's own `_meta.hostvars`: a host with
// no variables must be present with an empty map, or enumerating a host set
// from this loses hosts silently.
func TestFlattenHostsKeepsAVarLessHost(t *testing.T) {
	out := flattenHosts(map[string]map[string]any{
		"web-1": {"ansible_host": "10.0.0.4"},
		"db-1":  {},
	})
	if len(out) != 2 {
		t.Fatalf("both hosts must be present, got %v", out)
	}
	vars, ok := out["db-1"]
	if !ok {
		t.Fatal("a var-less host must be present")
	}
	if vars == nil || len(vars) != 0 {
		t.Errorf("a var-less host must carry an empty map, got %v", vars)
	}
}

// A group with no direct members reads as an empty list, not null, so a
// configuration indexing into it gets a usable value.
func TestOrEmptyListsNeverYieldsNil(t *testing.T) {
	out := orEmptyLists(map[string][]string{"web": {"web-1"}, "empty": nil})
	if out["empty"] == nil || len(out["empty"]) != 0 {
		t.Errorf("a nil list must become an empty one, got %v", out["empty"])
	}
	if len(out["web"]) != 1 {
		t.Errorf("a populated list must survive, got %v", out["web"])
	}
}

func TestReadIntoModelCarriesEverything(t *testing.T) {
	ctx := context.Background()
	m := &inventoryResolvedModel{}
	d := readIntoModel(ctx, &terrapod.ResolvedInventory{
		HostCount:  2,
		GroupCount: 2,
		Hosts: map[string]map[string]any{
			"web-1": {"ansible_host": "10.0.0.4", "ansible_port": float64(2222)},
			"db-1":  {},
		},
		Groups:        map[string][]string{"web": {"web-1"}, "eu": nil},
		GroupChildren: map[string][]string{"eu": {"web"}},
	}, m)
	if d.HasError() {
		t.Fatal(d)
	}

	if m.HostCount.ValueInt64() != 2 || m.GroupCount.ValueInt64() != 2 {
		t.Errorf("counts = %v %v", m.HostCount, m.GroupCount)
	}
	if len(m.Hosts.Elements()) != 2 {
		t.Errorf("hosts = %v", m.Hosts)
	}
	if len(m.Groups.Elements()) != 2 || len(m.GroupChildren.Elements()) != 1 {
		t.Errorf("groups = %v, children = %v", m.Groups, m.GroupChildren)
	}
}

// ── Driven through the data source ───────────────────────────────────────────

// A configured `limit` must reach the server. Resolving without it would
// silently answer a different question — the whole inventory rather than what
// the pattern targets.
func TestReadSendsAConfiguredLimit(t *testing.T) {
	ctx := context.Background()
	var gotQuery string
	d := &inventoryResolvedDataSource{tc: fakeClient(t, func(w http.ResponseWriter, req *http.Request) {
		gotQuery = req.URL.RawQuery
		writeResolved(w)
	})}

	resp := datasource.ReadResponse{State: emptyState(t)}
	d.Read(ctx, datasource.ReadRequest{Config: configWith(t, "ws-9", types.StringValue("web:!web-1"))}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("read: %v", resp.Diagnostics)
	}
	if gotQuery != "limit=web%3A%21web-1" {
		t.Errorf("the limit must reach the server, got query %q", gotQuery)
	}
}

// No limit means no query, not an empty one: an empty `--limit` is not the
// same request as none, and ansible would read it differently.
func TestReadWithoutALimitSendsNoQuery(t *testing.T) {
	ctx := context.Background()
	var gotQuery string
	d := &inventoryResolvedDataSource{tc: fakeClient(t, func(w http.ResponseWriter, req *http.Request) {
		gotQuery = req.URL.RawQuery
		writeResolved(w)
	})}

	resp := datasource.ReadResponse{State: emptyState(t)}
	d.Read(ctx, datasource.ReadRequest{Config: configWith(t, "ws-9", types.StringNull())}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("read: %v", resp.Diagnostics)
	}
	if gotQuery != "" {
		t.Errorf("no limit means no query, got %q", gotQuery)
	}
}

// Fails closed. A resolution error must be an error and not an empty host set:
// a configure targeting too little is worse than no answer, and unlike a
// policy gate there is no later evaluation to catch it.
func TestReadFailsClosedOnAResolutionError(t *testing.T) {
	ctx := context.Background()
	d := &inventoryResolvedDataSource{tc: fakeClient(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
	})}

	resp := datasource.ReadResponse{State: emptyState(t)}
	d.Read(ctx, datasource.ReadRequest{Config: configWith(t, "ws-9", types.StringNull())}, &resp)

	if !resp.Diagnostics.HasError() {
		t.Fatal("a resolution failure must be an error, never an empty inventory")
	}
	if !resp.State.Raw.IsNull() {
		t.Error("nothing must be written to state when the resolution failed")
	}
}

// ── Provenance (#2425 §4.1) ────────────────────────────────────────

// The parameter has to REACH the server, and alongside a limit rather than
// instead of it. The server reads an absent `provenance` as off, so a request
// that drops it answers 200 with no attribution at all -- `var_provenance`
// would simply be empty, which reads as "this inventory has no provenance"
// rather than as a lost parameter.
func TestReadSendsProvenanceAlongsideALimit(t *testing.T) {
	ctx := context.Background()
	var gotQuery string
	d := &inventoryResolvedDataSource{tc: fakeClient(t, func(w http.ResponseWriter, req *http.Request) {
		gotQuery = req.URL.RawQuery
		writeResolved(w)
	})}

	resp := datasource.ReadResponse{State: emptyState(t)}
	d.Read(ctx, datasource.ReadRequest{
		Config: configAsking(t, "ws-9", types.StringValue("web"), types.BoolValue(true)),
	}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("read: %v", resp.Diagnostics)
	}

	q, err := url.ParseQuery(gotQuery)
	if err != nil {
		t.Fatalf("query is not parseable: %q", gotQuery)
	}
	if q.Get("provenance") != "true" {
		t.Errorf("provenance must reach the server, got query %q", gotQuery)
	}
	// BOTH, which is the combination that motivated the options method: "these
	// are the hosts a configure would touch, and this is where each of their
	// variables came from" is one question, and asking it as two loses the join.
	if q.Get("limit") != "web" {
		t.Errorf("the limit must survive asking for provenance, got query %q", gotQuery)
	}
}

// `provenance = false` is not the same request as asking for it. Sending it
// anyway would cost every reader a second ansible invocation they declined.
func TestReadDoesNotAskForProvenanceWhenItIsFalse(t *testing.T) {
	ctx := context.Background()
	var gotQuery string
	d := &inventoryResolvedDataSource{tc: fakeClient(t, func(w http.ResponseWriter, req *http.Request) {
		gotQuery = req.URL.RawQuery
		writeResolved(w)
	})}

	resp := datasource.ReadResponse{State: emptyState(t)}
	d.Read(ctx, datasource.ReadRequest{
		Config: configAsking(t, "ws-9", types.StringNull(), types.BoolValue(false)),
	}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("read: %v", resp.Diagnostics)
	}
	if gotQuery != "" {
		t.Errorf("declining provenance must send no query, got %q", gotQuery)
	}
}

// The projection, against the shape the server really sends.
//
// Every level is a PLAIN group name. The server looks a group up as `@name`,
// because that is how `ansible-inventory --graph` spells one, and strips the
// sigil on the way out -- so a consumer that expected `@web` would match
// nothing while looking like it worked.
//
// And `here` is an ORIGIN, never a level: the chain carries only the ancestry
// that set the key, so a host overriding its group reports `origin=here` with
// the BEATEN group still in the chain. A projection that invented a `here`
// link to explain the winner would be inventing a level ansible never reported.
func TestProvenanceProjectsPlainLevelsAndEncodesNonStrings(t *testing.T) {
	ctx := context.Background()
	m := &inventoryResolvedModel{}
	d := readIntoModel(ctx, &terrapod.ResolvedInventory{
		HostCount: 1,
		Hosts:     map[string]map[string]any{"web-1": {"ansible_user": "deploy"}},
		VarProvenance: map[string]map[string]terrapod.VarOrigin{
			"web-1": {
				// Won at a group, beating a shallower one.
				"ansible_user": {
					Value:  "deploy",
					Origin: "web",
					Chain: []terrapod.VarOriginLevel{
						{Level: "all", Value: "root"},
						{Level: "web", Value: "deploy"},
					},
				},
				// Set on the host, beating a group: origin `here`, and the
				// beaten level is still in the chain.
				"ansible_port": {
					Value:  float64(2222),
					Origin: "here",
					Chain:  []terrapod.VarOriginLevel{{Level: "all", Value: "22"}},
				},
				// A container value, which a Terraform map cannot hold as
				// anything but a string.
				"tags": {Value: []any{"a", "b"}, Origin: "here", Chain: nil},
			},
		},
	}, m)
	if d.HasError() {
		t.Fatal(d)
	}

	hosts := m.VarProvenance.Elements()
	perHost, ok := hosts["web-1"].(types.Map)
	if !ok {
		t.Fatalf("web-1 is not a map of attributed variables: %T", hosts["web-1"])
	}
	vars := perHost.Elements()
	if len(vars) != 3 {
		t.Fatalf("want 3 attributed variables, got %v", vars)
	}

	for _, tc := range []struct {
		key    string
		value  string
		origin string
		levels []string
	}{
		{"ansible_user", "deploy", "web", []string{"all", "web"}},
		// A number is JSON-encoded, exactly as `hosts` renders it.
		{"ansible_port", "2222", "here", []string{"all"}},
		// No chain at all is an empty list, never null.
		{"tags", `["a","b"]`, "here", nil},
	} {
		t.Run(tc.key, func(t *testing.T) {
			obj, ok := vars[tc.key].(types.Object)
			if !ok {
				t.Fatalf("%s is not an object: %T", tc.key, vars[tc.key])
			}
			got := obj.Attributes()
			if v := got["value"].(types.String).ValueString(); v != tc.value {
				t.Errorf("value = %q, want %q", v, tc.value)
			}
			if o := got["origin"].(types.String).ValueString(); o != tc.origin {
				t.Errorf("origin = %q, want %q (a PLAIN level name, never @-prefixed)", o, tc.origin)
			}
			chain, ok := got["chain"].(types.List)
			if !ok {
				t.Fatalf("chain is not a list: %T", got["chain"])
			}
			if chain.IsNull() {
				t.Fatal("an empty chain must be an empty list, not null")
			}
			var levels []string
			for _, link := range chain.Elements() {
				levels = append(levels, link.(types.Object).
					Attributes()["level"].(types.String).ValueString())
			}
			if len(levels) != len(tc.levels) {
				t.Fatalf("chain levels = %v, want %v", levels, tc.levels)
			}
			// Order is ansible's precedence order, shallowest first, which is
			// what makes "everything origin beat" readable.
			for i := range levels {
				if levels[i] != tc.levels[i] {
					t.Errorf("chain[%d].level = %q, want %q", i, levels[i], tc.levels[i])
				}
			}
		})
	}
}

// An EMPTY map, never null. A configuration that forgot `provenance = true`
// then reads "no such key" from a lookup rather than erroring about a null
// collection -- the same reasoning every other collection here follows.
func TestProvenanceIsAnEmptyMapWhenItWasNotAsked(t *testing.T) {
	ctx := context.Background()
	m := &inventoryResolvedModel{}
	d := readIntoModel(ctx, &terrapod.ResolvedInventory{
		HostCount: 1,
		Hosts:     map[string]map[string]any{"web-1": {"ansible_host": "10.0.0.4"}},
	}, m)
	if d.HasError() {
		t.Fatal(d)
	}
	if m.VarProvenance.IsNull() {
		t.Fatal("var_provenance must be an empty map when provenance was not asked for, not null")
	}
	if len(m.VarProvenance.Elements()) != 0 {
		t.Errorf("want no attributed hosts, got %v", m.VarProvenance.Elements())
	}
}

// ── Helpers ──────────────────────────────────────────────────────────────────

func writeResolved(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "application/vnd.api+json")
	_, _ = w.Write([]byte(`{"data":{"id":"ws-9","type":"resolved-inventories","attributes":{
		"host-count":1,"group-count":1,
		"hosts":{"web-1":{"ansible_host":"10.0.0.4"}},
		"groups":{"web":["web-1"]},
		"group-children":{}},
		"relationships":{"workspace":{"data":{"id":"ws-9","type":"workspaces"}}}}}`))
}

func fakeClient(t *testing.T, h http.HandlerFunc) *terrapod.Client {
	t.Helper()
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: srv.URL, Token: "test-token"})
	if err != nil {
		t.Fatalf("building a client against the fake: %v", err)
	}
	return tc
}

func schemaOf(t *testing.T) (datasource.SchemaResponse, tftypes.Object) {
	t.Helper()
	ctx := context.Background()
	var resp datasource.SchemaResponse
	NewDataSource().Schema(ctx, datasource.SchemaRequest{}, &resp)
	objType, ok := resp.Schema.Type().TerraformType(ctx).(tftypes.Object)
	if !ok {
		t.Fatal("the schema's terraform type is not an object")
	}
	return resp, objType
}

// emptyState is a NULL state, so a test can tell "nothing was written"
// apart from "a zero-valued answer was written".
func emptyState(t *testing.T) tfsdk.State {
	t.Helper()
	resp, objType := schemaOf(t)
	return tfsdk.State{Schema: resp.Schema, Raw: tftypes.NewValue(objType, nil)}
}

// configWith builds the config a practitioner's block would produce: the two
// attributes they can write, and null for everything the server supplies.
//
// tfsdk.Config has no Set, so the raw value is assembled by hand. Filling only
// the writable attributes is also what keeps this honest -- a Computed
// attribute arriving pre-filled from the config would let Read pass while
// never having fetched anything.
func configWith(t *testing.T, workspaceID string, limit types.String) tfsdk.Config {
	t.Helper()
	resp, objType := schemaOf(t)
	attrs := make(map[string]tftypes.Value, len(objType.AttributeTypes))
	for name, ty := range objType.AttributeTypes {
		attrs[name] = tftypes.NewValue(ty, nil)
	}
	attrs["workspace_id"] = tftypes.NewValue(tftypes.String, workspaceID)
	if !limit.IsNull() {
		attrs["limit"] = tftypes.NewValue(tftypes.String, limit.ValueString())
	}
	return tfsdk.Config{Schema: resp.Schema, Raw: tftypes.NewValue(objType, attrs)}
}

// configAsking is configWith plus `provenance`, which is the third attribute a
// practitioner can write. Separate rather than a wider configWith so the
// existing cases keep asserting what they always did: a test that never
// mentions provenance should send a config where it is null, which is what a
// block omitting it produces.
func configAsking(t *testing.T, workspaceID string, limit types.String, provenance types.Bool) tfsdk.Config {
	t.Helper()
	resp, objType := schemaOf(t)
	attrs := make(map[string]tftypes.Value, len(objType.AttributeTypes))
	for name, ty := range objType.AttributeTypes {
		attrs[name] = tftypes.NewValue(ty, nil)
	}
	attrs["workspace_id"] = tftypes.NewValue(tftypes.String, workspaceID)
	if !limit.IsNull() {
		attrs["limit"] = tftypes.NewValue(tftypes.String, limit.ValueString())
	}
	if !provenance.IsNull() {
		attrs["provenance"] = tftypes.NewValue(tftypes.Bool, provenance.ValueBool())
	}
	return tfsdk.Config{Schema: resp.Schema, Raw: tftypes.NewValue(objType, attrs)}
}
