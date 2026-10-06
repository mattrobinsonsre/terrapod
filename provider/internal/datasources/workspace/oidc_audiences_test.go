package workspace

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
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
//
// And it is the data source, not the resource, that carries the EFFECTIVE set.
// The resource records only the keys a configuration declared, because its read
// has to agree with its own plan; a data source has no plan, so it can answer
// the merged view the workspace actually uses.
func TestDataSourceReadsTheMergedOIDCAudiences(t *testing.T) {
	ctx := context.Background()

	var m workspaceDataSourceModel
	res := wsResource(t, map[string]any{
		"name": "my-workspace",
		// As the server answers it: the workspace's own `aws` override, its
		// aliased `aws.west`, and `vault` inherited from the deployment's
		// audience catalogue.
		"oidc-audiences": map[string][]string{
			"aws":      {"sts.example.com", "api://example-exchange"},
			"aws.west": {"sts.example.com"},
			"vault":    {"https://vault.example.com"},
		},
	})
	if diags := readDataSourceModel(ctx, res, &m); diags.HasError() {
		t.Fatalf("read: %v", diags)
	}
	if m.OIDCAudiences.IsNull() || m.OIDCAudiences.IsUnknown() {
		t.Fatalf("oidc_audiences read back as %v, want a concrete map", m.OIDCAudiences)
	}
	got := m.OIDCAudiences.Elements()
	if len(got) != 3 {
		t.Fatalf("oidc_audiences read back %d keys, want 3 (including the inherited one): %v",
			len(got), got)
	}
	// The inherited key is the point: the data source must NOT narrow the way
	// the resource does, or a config reading it to build a trust policy would
	// miss a target the workspace genuinely mints for.
	if _, ok := got["vault"]; !ok {
		t.Error("the inherited `vault` key is missing — the data source is narrowing the " +
			"merged view, which is the resource's job and not this one's")
	}
	// An alias is part of the key, not a nested structure: `aws` and `aws.west`
	// are two independent provider configurations.
	if _, ok := got["aws.west"]; !ok {
		t.Error("the aliased `aws.west` key was not read back as its own key")
	}
	// Verbatim and in order: an audience is the federation target's own opaque
	// string, and several under one key mean "interchangeable", which some
	// targets refuse — so neither value nor order may be altered.
	aws := dsAudiences(t, got, "aws")
	if len(aws) != 2 || aws[0] != "sts.example.com" || aws[1] != "api://example-exchange" {
		t.Errorf("oidc_audiences[aws] = %q, want both entries verbatim and in order", aws)
	}
}

// A workspace that mints nothing reads back as null, matching every other
// collection on this data source, so a config interpolating it gets a usable
// zero rather than an error.
func TestDataSourceReadsAnEmptyOIDCAudienceMapAsNull(t *testing.T) {
	ctx := context.Background()

	var m workspaceDataSourceModel
	res := wsResource(t, map[string]any{
		"name": "my-workspace", "oidc-audiences": map[string][]string{},
	})
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
	ma, ok := a.(schema.MapAttribute)
	if !ok {
		t.Fatalf("oidc_audiences is %T, want schema.MapAttribute — a key is a provider "+
			"configuration name and the value is that target's audiences", a)
	}
	if ma.ElementType != (types.ListType{ElemType: types.StringType}) {
		t.Errorf("oidc_audiences element type is %v, want a list of strings", ma.ElementType)
	}
}

// dsAudiences pulls one key's audiences out of the read-back map.
func dsAudiences(t *testing.T, elems map[string]attr.Value, key string) []string {
	t.Helper()
	l, ok := elems[key].(types.List)
	if !ok {
		t.Fatalf("oidc_audiences[%s] is %T, want types.List", key, elems[key])
	}
	out := make([]string, 0, len(l.Elements()))
	for _, e := range l.Elements() {
		s, ok := e.(types.String)
		if !ok {
			t.Fatalf("oidc_audiences[%s] element %v is %T, want types.String", key, e, e)
		}
		out = append(out, s.ValueString())
	}
	return out
}
