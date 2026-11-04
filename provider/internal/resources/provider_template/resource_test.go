package provider_template

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/tfsdk"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-go/tftypes"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

func TestReadProviderTemplateUsesAuthoringValuesInsteadOfRedactedState(t *testing.T) {
	mutations := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.Method != http.MethodGet {
			mutations++
			_, _ = w.Write([]byte(`{"data":{"id":"id","type":"provider-templates","attributes":{"body":null,"parameters":[{"name":"token"}]}}}`))
			return
		}
		if req.URL.Query().Get("include-sensitive-values") != "true" {
			_, _ = w.Write([]byte(`{"data":{"id":"id","type":"provider-templates","attributes":{"body":null,"parameters":[{"name":"token"}]}}}`))
			return
		}
		_, _ = w.Write([]byte(`{"data":{"id":"id","type":"provider-templates","attributes":{"name":"aws","provider-type":"aws","body":"private","parameters":[{"name":"token","sensitive":true,"default":"private"}]}}}`))
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
	if diags := state.Set(ctx, &providerTemplateModel{ID: types.StringValue("id"), Labels: types.MapNull(types.StringType)}); diags.HasError() {
		t.Fatal(diags)
	}
	r := &providerTemplateResource{tc: tc}
	resp := resource.ReadResponse{State: state}
	r.Read(ctx, resource.ReadRequest{State: state}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatal(resp.Diagnostics)
	}
	var got providerTemplateModel
	if diags := resp.State.Get(ctx, &got); diags.HasError() {
		t.Fatal(diags)
	}
	if got.Body.ValueString() != "private" || got.ParametersJSON.ValueString() != `[{"default":"private","name":"token","sensitive":true}]` {
		t.Fatalf("refresh lost sensitive authoring values: body=%s parameters=%s", got.Body.ValueString(), got.ParametersJSON.ValueString())
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
		var value providerTemplateModel
		if diags := result.Get(ctx, &value); diags.HasError() || value.Body.ValueString() != "private" || value.ParametersJSON.ValueString() != got.ParametersJSON.ValueString() {
			t.Fatalf("write must retain authoring values in state: %v %+v", diags, value)
		}
	}
}
