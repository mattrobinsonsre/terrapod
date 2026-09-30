package workspaces

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// The engine filter is served server-side, but an unrecognised query parameter
// is IGNORED rather than refused — so a server predating `filter[engine]`
// answers an engine-narrowed request with every engine it has. The caller is
// about to treat that list as one engine's, which is the exact failure the
// filter exists to prevent, so the result is checked rather than trusted.
func TestWantEngine(t *testing.T) {
	cases := []struct {
		name string
		want string
		got  string
		keep bool
	}{
		{"no filter keeps everything", "", "pulumi", true},
		{"no filter keeps a blank engine too", "", "", true},
		{"terraform filter keeps terraform", "terraform", "terraform", true},
		{"terraform filter drops pulumi", "terraform", "pulumi", false},
		{"pulumi filter keeps pulumi", "pulumi", "pulumi", true},
		{"pulumi filter drops terraform", "pulumi", "terraform", false},
		// A server predating the `engine` attribute reports "" for every
		// workspace. That is Terraform — the column's default, and the only
		// engine such a server has — so it must survive a terraform filter and
		// must not leak into a pulumi one.
		{"blank engine counts as terraform", "terraform", "", true},
		{"blank engine is not pulumi", "pulumi", "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := wantEngine(tc.want, tc.got); got != tc.keep {
				t.Errorf("wantEngine(%q, %q) = %v, want %v", tc.want, tc.got, got, tc.keep)
			}
		})
	}
}

// The data source has to offer `engine` in both directions: as a filter
// argument on the list, and as a computed attribute on each element. One
// without the other is half a fix — a filter with no attribute leaves a
// caller unable to see what it got, and an attribute with no filter makes
// every caller re-implement the narrowing.
func TestSchemaCarriesEngineBothWays(t *testing.T) {
	var resp datasource.SchemaResponse
	NewDataSource().(*workspacesDataSource).Schema(context.Background(), datasource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema: %v", resp.Diagnostics)
	}

	filter, ok := resp.Schema.Attributes["engine"]
	if !ok {
		t.Fatal("data source has no top-level `engine` filter argument")
	}
	if !filter.IsOptional() {
		t.Error("`engine` filter must be optional — omitting it means every engine")
	}
	if filter.IsRequired() {
		t.Error("`engine` filter must not be required")
	}

	list, ok := resp.Schema.Attributes["workspaces"].(schema.ListNestedAttribute)
	if !ok {
		t.Fatal("`workspaces` is not a list-nested attribute")
	}
	elem, ok := list.NestedObject.Attributes["engine"]
	if !ok {
		t.Fatal("listed workspaces carry no `engine` attribute")
	}
	if !elem.IsComputed() {
		t.Error("the per-workspace `engine` must be computed")
	}
}

// The filter has to reach the SERVER — that is the mechanism, and the
// client-side drop is only a backstop. A filter that never leaves the provider
// still produces a correct-looking list on a single-engine instance, so the
// query parameter is asserted rather than inferred from the result.
func TestReadSendsTheEngineFilterToTheServer(t *testing.T) {
	var gotQuery string
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = io.WriteString(w, `{"data":[{"type":"workspaces","id":"ws-1",`+
			`"attributes":{"name":"stacks","engine":"pulumi","execution-mode":"agent"}}]}`)
	}))
	defer api.Close()

	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: api.URL, Token: "t"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	opts := listOptions(workspacesDataSourceModel{
		Search: types.StringNull(),
		Engine: types.StringValue("pulumi"),
	})
	if opts.Engine != "pulumi" {
		t.Fatalf("listOptions dropped the engine filter: %+v", opts)
	}

	got, err := listWorkspaces(context.Background(), tc, opts)
	if err != nil {
		t.Fatalf("listWorkspaces: %v", err)
	}
	if !strings.Contains(gotQuery, "filter%5Bengine%5D=pulumi") {
		t.Errorf("engine filter never reached the server; query was %q", gotQuery)
	}
	if len(got) != 1 || got[0].Engine.ValueString() != "pulumi" {
		t.Errorf("listed %d workspaces; want one pulumi workspace", len(got))
	}
}

// And when the server ignores the parameter — an older server that predates it
// — the caller must still get only what it asked for, because it is about to
// treat the result as one engine's.
func TestReadDropsWhatAServerThatIgnoredTheFilterReturns(t *testing.T) {
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = io.WriteString(w, `{"data":[`+
			`{"type":"workspaces","id":"ws-1","attributes":{"name":"infra","engine":"terraform"}},`+
			`{"type":"workspaces","id":"ws-2","attributes":{"name":"stacks","engine":"pulumi"}}]}`)
	}))
	defer api.Close()

	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: api.URL, Token: "t"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	got, err := listWorkspaces(context.Background(), tc,
		terrapod.WorkspaceListOptions{Engine: "terraform"})
	if err != nil {
		t.Fatalf("listWorkspaces: %v", err)
	}
	if len(got) != 1 || got[0].Name.ValueString() != "infra" {
		t.Fatalf("a Pulumi workspace survived a terraform-filtered list: %d kept", len(got))
	}
}

// No filter means every engine — an operator listing the estate must see all
// of it, not a silently Terraform-shaped slice.
func TestReadWithoutAFilterReturnsEveryEngine(t *testing.T) {
	var gotQuery string
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = io.WriteString(w, `{"data":[`+
			`{"type":"workspaces","id":"ws-1","attributes":{"name":"infra","engine":"terraform"}},`+
			`{"type":"workspaces","id":"ws-2","attributes":{"name":"stacks","engine":"pulumi"}}]}`)
	}))
	defer api.Close()

	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: api.URL, Token: "t"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	opts := listOptions(workspacesDataSourceModel{Search: types.StringNull(), Engine: types.StringNull()})
	got, err := listWorkspaces(context.Background(), tc, opts)
	if err != nil {
		t.Fatalf("listWorkspaces: %v", err)
	}
	if strings.Contains(gotQuery, "filter") {
		t.Errorf("an unfiltered list sent a filter: %q", gotQuery)
	}
	if len(got) != 2 {
		t.Errorf("returned %d workspaces, want both engines", len(got))
	}
}
