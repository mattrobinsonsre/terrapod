package provider

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	fwprovider "github.com/hashicorp/terraform-plugin-framework/provider"
	"github.com/hashicorp/terraform-plugin-framework/providerserver"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-go/tfprotov6"
	"github.com/hashicorp/terraform-plugin-go/tftypes"
)

// A Vault reference that asks for file delivery (#1619), driven through the
// provider's real protocol server the way Terraform core drives it:
//
//	plan → apply → refresh → plan (must be empty) → change file.name →
//	plan (must be an in-place update) → apply → refresh → plan (empty)
//
// against a fake Terrapod. Two things are pinned. A `file` reference reads back
// without drift — a vault reference is sensitive, and a read that skipped it
// would leave the reference permanently unapplied. And renaming the file is an
// update, not a replace: a replace would delete the variable and re-create it,
// which breaks every run queued in between.

const (
	cycleRef     = `{"source":"vault","mount":"secret","path":"apps/gcp","field":"sa_json","file":{"name":"gcp/adc.json"}}`
	cycleRenamed = `{"source":"vault","mount":"secret","path":"apps/gcp","field":"sa_json","file":{"name":"~/.config/gcloud/adc.json"}}`
)

// fakeTerrapodVar serves a single variable at <collection> and counts writes.
type fakeTerrapodVar struct {
	mu                      sync.Mutex
	attrs                   map[string]any
	posts, patches, deletes int
}

func (f *fakeTerrapodVar) serve(t *testing.T, collection string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		w.Header().Set("Content-Type", "application/vnd.api+json")

		decode := func() map[string]any {
			raw, _ := io.ReadAll(r.Body)
			var body struct {
				Data struct {
					Attributes map[string]any `json:"attributes"`
				} `json:"data"`
			}
			if err := json.Unmarshal(raw, &body); err != nil {
				t.Errorf("write body is not JSON: %v", err)
			}
			return body.Data.Attributes
		}
		// What Terrapod returns: a vault variable is forced sensitive and hands
		// back the reference itself in `value` rather than a mask; any other
		// sensitive value is redacted. updated-at and version-id move on every
		// write, as they do on the real server, so a plan that wrongly expects
		// them to change is caught rather than satisfied by a frozen fake.
		resource := func() map[string]any {
			source, _ := f.attrs["value-source"].(string)
			if source == "" {
				source = "static"
			}
			sensitive := source == "vault" || f.attrs["sensitive"] == true
			value := f.attrs["value"]
			if sensitive && source != "vault" {
				value = ""
			}
			stamp := fmt.Sprintf("2026-09-15T00:00:%02dZ", f.posts+f.patches)
			return map[string]any{"id": "var-1", "type": "vars", "attributes": map[string]any{
				"key": f.attrs["key"], "category": f.attrs["category"], "hcl": false,
				"sensitive": sensitive, "value-source": source, "value": value,
				"version-id": fmt.Sprintf("v%d", f.posts+f.patches),
				"created-at": "2026-09-15T00:00:00Z", "updated-at": stamp,
			}}
		}

		switch {
		case r.Method == http.MethodPost && r.URL.Path == collection:
			f.posts++
			f.attrs = decode()
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(map[string]any{"data": resource()})
		case r.Method == http.MethodGet && r.URL.Path == collection:
			list := []any{}
			if f.attrs != nil {
				list = append(list, resource())
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"data": list})
		case r.Method == http.MethodPatch && r.URL.Path == collection+"/var-1":
			f.patches++
			for k, v := range decode() {
				f.attrs[k] = v
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"data": resource()})
		case r.Method == http.MethodDelete && r.URL.Path == collection+"/var-1":
			f.deletes++
			f.attrs = nil
			w.WriteHeader(http.StatusNoContent)
		default:
			// Includes the version probe at /.well-known — a failed probe is a
			// warning at most, never a failure, which is what we want here.
			http.Error(w, "not found", http.StatusNotFound)
		}
	}
}

// protoHarness drives one resource type through the protocol server.
type protoHarness struct {
	t        *testing.T
	ctx      context.Context
	srv      tfprotov6.ProviderServer
	typeName string
	computed map[string]bool
	typ      tftypes.Type
}

func newProtoHarness(t *testing.T, baseURL, typeName string) *protoHarness {
	t.Helper()
	ctx := context.Background()
	p := New("dev")()
	srv, err := providerserver.NewProtocol6WithError(p)()
	if err != nil {
		t.Fatalf("provider server: %v", err)
	}

	// The value types come from the framework schemas directly rather than
	// from GetProviderSchema, which validates every resource in the provider
	// at once. This test is about one resource's cycle; a schema problem
	// elsewhere is not something it should report, or hide.
	var ps fwprovider.SchemaResponse
	p.Schema(ctx, fwprovider.SchemaRequest{}, &ps)
	provTyp := ps.Schema.Type().TerraformType(ctx)

	var rs resource.SchemaResponse
	found := false
	for _, factory := range p.Resources(ctx) {
		r := factory()
		var md resource.MetadataResponse
		r.Metadata(ctx, resource.MetadataRequest{ProviderTypeName: providerTypeName}, &md)
		if md.TypeName == typeName {
			r.Schema(ctx, resource.SchemaRequest{}, &rs)
			found = true
			break
		}
	}
	if !found {
		t.Fatalf("no resource %s", typeName)
	}
	if rs.Diagnostics.HasError() {
		t.Fatalf("%s schema: %v", typeName, rs.Diagnostics)
	}
	computed := map[string]bool{}
	for name, a := range rs.Schema.Attributes {
		computed[name] = a.IsComputed()
	}

	cfg := objectWith(provTyp, map[string]tftypes.Value{
		"hostname": tftypes.NewValue(tftypes.String, baseURL),
		"token":    tftypes.NewValue(tftypes.String, "t"),
	})
	cfgDV := dynamic(t, provTyp, cfg)
	cr, err := srv.ConfigureProvider(ctx, &tfprotov6.ConfigureProviderRequest{
		TerraformVersion: "1.9.0", Config: &cfgDV,
	})
	if err != nil {
		t.Fatalf("ConfigureProvider: %v", err)
	}
	failOnDiags(t, "ConfigureProvider", cr.Diagnostics)

	return &protoHarness{
		t: t, ctx: ctx, srv: srv, typeName: typeName, computed: computed,
		typ: rs.Schema.Type().TerraformType(ctx),
	}
}

// config builds the resource's configuration: the given attributes, null
// elsewhere, as an operator's HCL would.
func (h *protoHarness) config(set map[string]tftypes.Value) tftypes.Value {
	return objectWith(h.typ, set)
}

// proposed is Terraform core's proposed new state: configured values win,
// and a computed attribute left out of the config keeps its prior value.
func (h *protoHarness) proposed(prior, config tftypes.Value) tftypes.Value {
	if prior.IsNull() {
		return config
	}
	var p, c map[string]tftypes.Value
	if err := prior.As(&p); err != nil {
		h.t.Fatalf("prior: %v", err)
	}
	if err := config.As(&c); err != nil {
		h.t.Fatalf("config: %v", err)
	}
	out := map[string]tftypes.Value{}
	for name, isComputed := range h.computed {
		if !c[name].IsNull() || !isComputed {
			out[name] = c[name]
		} else {
			out[name] = p[name]
		}
	}
	return tftypes.NewValue(h.typ, out)
}

func (h *protoHarness) plan(prior, config tftypes.Value) *tfprotov6.PlanResourceChangeResponse {
	h.t.Helper()
	priorDV, propDV, cfgDV := dynamic(h.t, h.typ, prior), dynamic(h.t, h.typ, h.proposed(prior, config)), dynamic(h.t, h.typ, config)
	resp, err := h.srv.PlanResourceChange(h.ctx, &tfprotov6.PlanResourceChangeRequest{
		TypeName: h.typeName, PriorState: &priorDV, ProposedNewState: &propDV, Config: &cfgDV,
	})
	if err != nil {
		h.t.Fatalf("PlanResourceChange: %v", err)
	}
	failOnDiags(h.t, "PlanResourceChange", resp.Diagnostics)
	return resp
}

func (h *protoHarness) apply(prior, config tftypes.Value, pr *tfprotov6.PlanResourceChangeResponse) tftypes.Value {
	h.t.Helper()
	priorDV, cfgDV := dynamic(h.t, h.typ, prior), dynamic(h.t, h.typ, config)
	resp, err := h.srv.ApplyResourceChange(h.ctx, &tfprotov6.ApplyResourceChangeRequest{
		TypeName: h.typeName, PriorState: &priorDV, PlannedState: pr.PlannedState,
		Config: &cfgDV, PlannedPrivate: pr.PlannedPrivate,
	})
	if err != nil {
		h.t.Fatalf("ApplyResourceChange: %v", err)
	}
	failOnDiags(h.t, "ApplyResourceChange", resp.Diagnostics)
	return h.value(resp.NewState)
}

func (h *protoHarness) refresh(state tftypes.Value) tftypes.Value {
	h.t.Helper()
	dv := dynamic(h.t, h.typ, state)
	resp, err := h.srv.ReadResource(h.ctx, &tfprotov6.ReadResourceRequest{TypeName: h.typeName, CurrentState: &dv})
	if err != nil {
		h.t.Fatalf("ReadResource: %v", err)
	}
	failOnDiags(h.t, "ReadResource", resp.Diagnostics)
	return h.value(resp.NewState)
}

func (h *protoHarness) value(dv *tfprotov6.DynamicValue) tftypes.Value {
	h.t.Helper()
	v, err := dv.Unmarshal(h.typ)
	if err != nil {
		h.t.Fatalf("decode state: %v", err)
	}
	return v
}

func (h *protoHarness) attr(v tftypes.Value, name string) string {
	h.t.Helper()
	var m map[string]tftypes.Value
	if err := v.As(&m); err != nil {
		h.t.Fatalf("state: %v", err)
	}
	var s string
	if err := m[name].As(&s); err != nil {
		h.t.Fatalf("%s: %v", name, err)
	}
	return s
}

func objectWith(typ tftypes.Type, set map[string]tftypes.Value) tftypes.Value {
	obj := typ.(tftypes.Object)
	vals := map[string]tftypes.Value{}
	for name, at := range obj.AttributeTypes {
		if v, ok := set[name]; ok {
			vals[name] = v
		} else {
			vals[name] = tftypes.NewValue(at, nil)
		}
	}
	return tftypes.NewValue(typ, vals)
}

func dynamic(t *testing.T, typ tftypes.Type, v tftypes.Value) tfprotov6.DynamicValue {
	t.Helper()
	dv, err := tfprotov6.NewDynamicValue(typ, v)
	if err != nil {
		t.Fatalf("encode: %v", err)
	}
	return dv
}

func failOnDiags(t *testing.T, step string, diags []*tfprotov6.Diagnostic) {
	t.Helper()
	for _, d := range diags {
		if d.Severity == tfprotov6.DiagnosticSeverityError {
			t.Fatalf("%s: %s: %s", step, d.Summary, d.Detail)
		}
	}
}

// The control for the test below: an ordinary static variable through the
// same harness. If this drifts, the harness is wrong, not the provider.
func TestTheCycleHarnessSeesNoDriftOnAStaticVariable(t *testing.T) {
	fake := &fakeTerrapodVar{}
	api := httptest.NewServer(fake.serve(t, "/api/v2/workspaces/ws-1/vars"))
	defer api.Close()
	h := newProtoHarness(t, api.URL, "terrapod_variable")

	config := func(v string) tftypes.Value {
		return h.config(map[string]tftypes.Value{
			"workspace_id": tftypes.NewValue(tftypes.String, "ws-1"),
			"key":          tftypes.NewValue(tftypes.String, "aws_region"),
			"category":     tftypes.NewValue(tftypes.String, "terraform"),
			"value":        tftypes.NewValue(tftypes.String, v),
		})
	}
	none := tftypes.NewValue(h.typ, nil)
	cfg := config("eu-west-1")
	state := h.refresh(h.apply(none, cfg, h.plan(none, cfg)))
	if pr := h.plan(state, cfg); !h.value(pr.PlannedState).Equal(state) {
		t.Fatalf("a static variable drifts: %v", h.value(pr.PlannedState))
	}
	cfg2 := config("us-east-1")
	pr := h.plan(state, cfg2)
	if len(pr.RequiresReplace) != 0 {
		t.Fatalf("changing a value forces a replace of %v", pr.RequiresReplace)
	}
	state = h.refresh(h.apply(state, cfg2, pr))
	if fake.posts != 1 || fake.patches != 1 || h.attr(state, "value") != "us-east-1" {
		t.Fatalf("writes %d/%d, value %q", fake.posts, fake.patches, h.attr(state, "value"))
	}
}

func TestAVaultFileReferenceCyclesWithoutDriftAndRenamesInPlace(t *testing.T) {
	cases := []struct {
		typeName, scopeAttr, scopeID, collection string
	}{
		{"terrapod_variable", "workspace_id", "ws-1", "/api/v2/workspaces/ws-1/vars"},
		{"terrapod_variable_set_variable", "varset_id", "varset-1", "/api/v2/varsets/varset-1/relationships/vars"},
	}
	for _, tc := range cases {
		t.Run(tc.typeName, func(t *testing.T) {
			fake := &fakeTerrapodVar{}
			api := httptest.NewServer(fake.serve(t, tc.collection))
			defer api.Close()
			h := newProtoHarness(t, api.URL, tc.typeName)

			config := func(ref string) tftypes.Value {
				return h.config(map[string]tftypes.Value{
					tc.scopeAttr:   tftypes.NewValue(tftypes.String, tc.scopeID),
					"key":          tftypes.NewValue(tftypes.String, "GOOGLE_APPLICATION_CREDENTIALS"),
					"category":     tftypes.NewValue(tftypes.String, "env"),
					"value_source": tftypes.NewValue(tftypes.String, "vault"),
					"value":        tftypes.NewValue(tftypes.String, ref),
				})
			}
			none := tftypes.NewValue(h.typ, nil)

			// Create.
			cfg := config(cycleRef)
			created := h.apply(none, cfg, h.plan(none, cfg))
			if got := h.attr(created, "value"); got != cycleRef {
				t.Fatalf("state after create holds %q, want the reference", got)
			}
			if fake.attrs["value"] != cycleRef {
				t.Fatalf("the API was sent %v, want the reference verbatim", fake.attrs["value"])
			}

			// Refresh, then plan again: nothing may change.
			state := h.refresh(created)
			if !state.Equal(created) {
				t.Fatalf("refresh changed the state:\n  %v\nto\n  %v", created, state)
			}
			if pr := h.plan(state, cfg); len(pr.RequiresReplace) != 0 || !h.value(pr.PlannedState).Equal(state) {
				t.Fatalf("drift after refresh: planned %v (replace %v)", h.value(pr.PlannedState), pr.RequiresReplace)
			}

			// Rename the file: an in-place update, never a replace.
			cfg2 := config(cycleRenamed)
			pr := h.plan(state, cfg2)
			if len(pr.RequiresReplace) != 0 {
				t.Fatalf("renaming the file forces a replace of %v", pr.RequiresReplace)
			}
			if h.value(pr.PlannedState).Equal(state) {
				t.Fatal("renaming the file planned no change")
			}
			updated := h.apply(state, cfg2, pr)
			if fake.posts != 1 || fake.patches != 1 || fake.deletes != 0 {
				t.Fatalf("writes: %d create, %d update, %d delete; want 1, 1, 0", fake.posts, fake.patches, fake.deletes)
			}
			if fake.attrs["value"] != cycleRenamed {
				t.Fatalf("the API holds %v after the update", fake.attrs["value"])
			}

			// And the renamed reference settles too.
			state = h.refresh(updated)
			if got := h.attr(state, "value"); got != cycleRenamed {
				t.Fatalf("refresh read back %q", got)
			}
			if pr := h.plan(state, cfg2); len(pr.RequiresReplace) != 0 || !h.value(pr.PlannedState).Equal(state) {
				t.Fatalf("drift after the update: planned %v", h.value(pr.PlannedState))
			}
			if !strings.Contains(h.attr(state, "value"), `"file":{"name":"~/.config/gcloud/adc.json"}`) {
				t.Fatal("the file object did not survive the cycle")
			}
		})
	}
}
