package inventory_resolved

import (
	"context"
	"net/http"
	"net/http/httptest"
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
		"workspace_id", "limit", "hosts", "groups", "group_children", "host_count", "group_count",
	} {
		if _, ok := attrs[name]; !ok {
			t.Errorf("attribute %q is missing", name)
		}
	}
	if len(attrs) != 7 {
		t.Errorf("unexpected attribute count %d: %v", len(attrs), attrs)
	}

	if !attrs["workspace_id"].IsRequired() {
		t.Error("workspace_id is required")
	}
	if !attrs["limit"].IsOptional() || attrs["limit"].IsRequired() {
		t.Error("limit is optional")
	}
	for _, name := range []string{"hosts", "groups", "group_children", "host_count", "group_count"} {
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
