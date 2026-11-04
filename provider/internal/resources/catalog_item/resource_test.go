package catalog_item

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/tfsdk"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-go/tftypes"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

func TestCatalogItemSelfContainedCreateUpdateAndRead(t *testing.T) {
	ctx := context.Background()
	constraints := map[string]string{"tofu": ">=1.8,<2", "terraform": ">=1.6,<2"}
	value, diags := types.MapValueFrom(ctx, types.StringType, constraints)
	if diags.HasError() {
		t.Fatal(diags)
	}
	plan := &catalogItemModel{
		Name: types.StringValue("example"), ModuleID: types.StringValue("module-id"),
		SelfContained: types.BoolValue(true), ExecutionBackendConstraints: value,
	}
	for _, update := range []bool{false, true} {
		attrs, diags := buildCatalogItemAttrs(plan, update)
		if diags.HasError() {
			t.Fatal(diags)
		}
		if attrs["self-contained"] != true || !reflect.DeepEqual(attrs["execution-backend-constraints"], constraints) {
			t.Fatalf("update=%t: wrong request attributes: %v", update, attrs)
		}
		// The SDK decodes JSON:API objects as map[string]any, not map[string]string.
		ci := &terrapod.CatalogItem{ID: "item-id", Attributes: map[string]any{
			"name": "example", "module-id": "module-id", "self-contained": true,
			"execution-backend-constraints": map[string]any{"tofu": ">=1.8,<2", "terraform": ">=1.6,<2"},
		}}
		imported := &catalogItemModel{}
		if diags := readCatalogItemIntoModel(ctx, ci, imported); diags.HasError() {
			t.Fatal(diags)
		}
		if imported.ID.ValueString() != "item-id" || imported.SelfContained.IsNull() || !imported.SelfContained.ValueBool() {
			t.Fatalf("imported self-contained item: %+v", imported)
		}
		got, diags := buildCatalogItemAttrs(imported, update)
		if diags.HasError() || !reflect.DeepEqual(got["execution-backend-constraints"], constraints) {
			t.Fatalf("roundtrip update=%t: attrs=%v diags=%v", update, got, diags)
		}
	}
}

func TestCatalogItemWrapperAndExplicitClearing(t *testing.T) {
	ctx := context.Background()
	plan := &catalogItemModel{
		Name: types.StringValue("example"), ModuleID: types.StringValue("module-id"),
		SelfContained: types.BoolNull(), ExecutionBackendConstraints: types.MapNull(types.StringType),
	}
	created, diags := buildCatalogItemAttrs(plan, false)
	if diags.HasError() {
		t.Fatal(diags)
	}
	if _, ok := created["self-contained"]; ok {
		t.Fatalf("wrapper create should omit mode: %v", created)
	}
	if _, ok := created["execution-backend-constraints"]; ok {
		t.Fatalf("wrapper create should omit constraints: %v", created)
	}
	plan.SelfContained = types.BoolValue(false)
	updated, diags := buildCatalogItemAttrs(plan, true)
	if diags.HasError() {
		t.Fatal(diags)
	}
	if updated["self-contained"] != false {
		t.Fatalf("explicit false must be sent: %v", updated)
	}
	if v, ok := updated["execution-backend-constraints"]; !ok || v != nil {
		t.Fatalf("removing constraints must send explicit null: %v", updated)
	}
	wire, err := terrapod.MarshalResourceWithID("item-id", "catalog-items", updated)
	if err != nil {
		t.Fatal(err)
	}
	var body struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(wire, &body); err != nil {
		t.Fatal(err)
	}
	if body.Data.Attributes["self-contained"] != false {
		t.Fatalf("wire must contain false: %s", wire)
	}
	if v, ok := body.Data.Attributes["execution-backend-constraints"]; !ok || v != nil {
		t.Fatalf("wire must contain explicit null: %s", wire)
	}
	ci := &terrapod.CatalogItem{Attributes: map[string]any{
		"self-contained": false, "execution-backend-constraints": nil,
	}}
	if diags := readCatalogItemIntoModel(ctx, ci, plan); diags.HasError() {
		t.Fatal(diags)
	}
	if plan.SelfContained.IsNull() || plan.SelfContained.ValueBool() || !plan.ExecutionBackendConstraints.IsNull() {
		t.Fatalf("wrapper read must preserve false and null: %+v", plan)
	}
}

func TestCatalogItemRejectsInvalidConstraints(t *testing.T) {
	ctx := context.Background()
	for _, input := range []map[string]string{{}, {"other": ">=1"}} {
		value, diags := types.MapValueFrom(ctx, types.StringType, input)
		if diags.HasError() {
			t.Fatal(diags)
		}
		plan := &catalogItemModel{ExecutionBackendConstraints: value}
		if _, diags := buildCatalogItemAttrs(plan, false); !diags.HasError() {
			t.Fatalf("expected validation error for %v", input)
		}
	}
	plan := &catalogItemModel{
		SelfContained: types.BoolValue(true), ExecutionBackendConstraints: types.MapNull(types.StringType),
	}
	if _, diags := buildCatalogItemAttrs(plan, false); !diags.HasError() {
		t.Fatal("self-contained items must require constraints")
	}
}

func TestReadCatalogItemUsesAuthoringValuesInsteadOfRedactedState(t *testing.T) {
	secret := "private-default"
	mutations := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.Method != http.MethodGet {
			mutations++
			_, _ = w.Write([]byte(`{"data":{"id":"id","type":"catalog-items","attributes":{"variable-options":[{"name":"token"}]}}}`))
			return
		}
		if req.URL.Query().Get("include-sensitive-values") != "true" {
			_, _ = w.Write([]byte(`{"data":{"id":"id","type":"catalog-items","attributes":{"variable-options":[{"name":"token"}]}}}`))
			return
		}
		_, _ = w.Write([]byte(`{"data":{"id":"id","type":"catalog-items","attributes":{"name":"example","module-id":"module-id","variable-options":[{"name":"token","sensitive":true,"default":"` + secret + `"}]}}}`))
	}))
	t.Cleanup(srv.Close)
	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: srv.URL, Token: "admin"})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	var sch resource.SchemaResponse
	NewResource().Schema(ctx, resource.SchemaRequest{}, &sch)
	obj := sch.Schema.Type().TerraformType(ctx).(tftypes.Object)
	values := map[string]tftypes.Value{}
	for name, ty := range obj.AttributeTypes {
		values[name] = tftypes.NewValue(ty, nil)
	}
	state := tfsdk.State{Schema: sch.Schema, Raw: tftypes.NewValue(obj, values)}
	if diags := state.Set(ctx, &catalogItemModel{
		ID: types.StringValue("id"), ProviderTemplateIDs: types.ListNull(types.StringType),
		AllowedAgentPoolIDs:         types.ListNull(types.StringType),
		ExecutionBackendConstraints: types.MapNull(types.StringType), Labels: types.MapNull(types.StringType),
	}); diags.HasError() {
		t.Fatal(diags)
	}
	r := &catalogItemResource{tc: tc}
	resp := resource.ReadResponse{State: state}
	r.Read(ctx, resource.ReadRequest{State: state}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatal(resp.Diagnostics)
	}
	var got catalogItemModel
	if diags := resp.State.Get(ctx, &got); diags.HasError() {
		t.Fatal(diags)
	}
	if !containsValue(got.VariableOptionsJSON.ValueString(), secret) {
		t.Fatalf("refresh lost secret from Terraform state: %s", got.VariableOptionsJSON.ValueString())
	}
	plan := tfsdk.Plan{Schema: sch.Schema, Raw: resp.State.Raw}
	created := resource.CreateResponse{State: state}
	r.Create(ctx, resource.CreateRequest{Plan: plan}, &created)
	if created.Diagnostics.HasError() {
		t.Fatal(created.Diagnostics)
	}
	updated := resource.UpdateResponse{State: created.State}
	r.Update(ctx, resource.UpdateRequest{Plan: plan, State: created.State}, &updated)
	if updated.Diagnostics.HasError() {
		t.Fatal(updated.Diagnostics)
	}
	if mutations != 2 {
		t.Fatalf("expected create and update, got %d mutations", mutations)
	}
	for _, result := range []tfsdk.State{created.State, updated.State} {
		var value catalogItemModel
		if diags := result.Get(ctx, &value); diags.HasError() || !containsValue(value.VariableOptionsJSON.ValueString(), secret) {
			t.Fatalf("write must retain authoring value in state: %v %+v", diags, value)
		}
	}
}

func containsValue(raw, value string) bool {
	var decoded []map[string]any
	return json.Unmarshal([]byte(raw), &decoded) == nil && len(decoded) == 1 && decoded[0]["default"] == value
}
