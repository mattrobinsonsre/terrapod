package workspace

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/types"
	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

func wsResource(t *testing.T, attrs map[string]any) *terrapod.Resource {
	t.Helper()
	raw := map[string]json.RawMessage{}
	for k, v := range attrs {
		b, err := json.Marshal(v)
		if err != nil {
			t.Fatalf("marshal %s: %v", k, err)
		}
		raw[k] = b
	}
	return &terrapod.Resource{ID: "ws-1", Type: "workspaces", Attributes: raw}
}

// The data source exists so a config can READ a workspace it does not manage —
// including which audiences its runs mint an identity token for (#1901), which
// is the thing an operator cross-references against their own federation trust
// policy. A workspace attribute the data source cannot see is invisible to
// exactly the config that would use it to wire the two together.
func TestDataSourceReadsOIDCAudiences(t *testing.T) {
	ctx := context.Background()

	var m workspaceDataSourceModel
	res := wsResource(t, map[string]any{
		"name":           "my-workspace",
		"oidc-audiences": []string{"sts.example.com", "api://example-exchange"},
	})
	if diags := readDataSourceModel(ctx, res, &m); diags.HasError() {
		t.Fatalf("read: %v", diags)
	}
	if m.OIDCAudiences.IsNull() || m.OIDCAudiences.IsUnknown() {
		t.Fatalf("oidc_audiences read back as %v, want a concrete list", m.OIDCAudiences)
	}
	elems := m.OIDCAudiences.Elements()
	if len(elems) != 2 {
		t.Fatalf("oidc_audiences read back with %d elements, want 2", len(elems))
	}
	// Verbatim: an audience is the federation target's own opaque string.
	if got := elems[0].(types.String).ValueString(); got != "sts.example.com" {
		t.Errorf("first audience = %q, want sts.example.com", got)
	}
	if got := elems[1].(types.String).ValueString(); got != "api://example-exchange" {
		t.Errorf("second audience = %q, want api://example-exchange", got)
	}
}

// A workspace that mints nothing reads back as null, matching every other
// collection on this data source, so a config interpolating it gets a usable
// zero rather than an error.
func TestDataSourceReadsAnEmptyOIDCAudienceListAsNull(t *testing.T) {
	ctx := context.Background()

	var m workspaceDataSourceModel
	res := wsResource(t, map[string]any{"name": "my-workspace", "oidc-audiences": []string{}})
	if diags := readDataSourceModel(ctx, res, &m); diags.HasError() {
		t.Fatalf("read: %v", diags)
	}
	if !m.OIDCAudiences.IsNull() {
		t.Errorf("oidc_audiences = %v, want null for a workspace that mints nothing",
			m.OIDCAudiences)
	}
}

func TestDataSourceOIDCAudiencesIsComputedOnly(t *testing.T) {
	var resp datasource.SchemaResponse
	NewDataSource().Schema(context.Background(), datasource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema build error: %v", resp.Diagnostics)
	}

	a, ok := resp.Schema.Attributes["oidc_audiences"]
	if !ok {
		t.Fatal("oidc_audiences attribute is missing from the terrapod_workspace data source")
	}
	if !a.IsComputed() {
		t.Error("oidc_audiences must be Computed on a data source")
	}
	if a.IsOptional() || a.IsRequired() {
		t.Error("oidc_audiences is read-only on a data source; it must be neither Optional nor Required")
	}
}
