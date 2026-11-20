package autodiscovery_rule

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/tfsdk"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-go/tftypes"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// Does `Update` actually SEND the propagate option?
//
// `propagate_test.go` drives `buildAutodiscoveryRuleAttrs` and
// `MarshalResourceWithOptions` directly, so deleting the options block from
// `Update` entirely leaves all three of its tests green and makes
// `propagate = true` in HCL a silent no-op that reports success. The helpers
// were mutation-checked when #2650 landed; the CALL SITE was not, which is the
// one place the feature can go missing.
//
// So this drives the real `Update` against an httptest fake and asserts what
// reaches the wire, the way `variable_set/update_test.go` and the MCP sibling
// (`mcp/internal/mcpserver/autodiscovery_tools_test.go`) already do.

type wiringFake struct {
	mu       sync.Mutex
	requests []string
	// top is the request document MINUS `data`: a request option travels at the
	// document top level beside it, so there is no `options` envelope to read.
	top map[string]any
}

func (f *wiringFake) handler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		f.requests = append(f.requests, r.Method)
		if r.Method == http.MethodPatch {
			raw, _ := io.ReadAll(r.Body)
			var doc map[string]json.RawMessage
			_ = json.Unmarshal(raw, &doc)
			delete(doc, "data")
			f.top = map[string]any{}
			for k, v := range doc {
				var parsed any
				_ = json.Unmarshal(v, &parsed)
				f.top[k] = parsed
			}
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"data": map[string]any{
			"id": "ad-1", "type": "autodiscovery-rules",
			"attributes": map[string]any{"name": "monorepo", "pattern": "services/*"},
		}})
	}
}

func newWiringResource(t *testing.T, f *wiringFake) *autodiscoveryRuleResource {
	t.Helper()
	srv := httptest.NewServer(f.handler())
	t.Cleanup(srv.Close)
	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: srv.URL, Token: "test-token"})
	if err != nil {
		t.Fatalf("building a client against the fake: %v", err)
	}
	return &autodiscoveryRuleResource{tc: tc}
}

// runWiringUpdate builds a state whose every attribute is null, sets only what
// the test cares about, and drives the real `Update`. Null-by-default matters:
// the model carries dozens of attributes and naming them all would make this
// test a second copy of the schema.
func runWiringUpdate(t *testing.T, r *autodiscoveryRuleResource, propagate types.Bool) resource.UpdateResponse {
	t.Helper()
	ctx := context.Background()

	var sr resource.SchemaResponse
	NewResource().Schema(ctx, resource.SchemaRequest{}, &sr)
	objType := sr.Schema.Type().TerraformType(ctx).(tftypes.Object)
	attrs := make(map[string]tftypes.Value, len(objType.AttributeTypes))
	for name, ty := range objType.AttributeTypes {
		attrs[name] = tftypes.NewValue(ty, nil)
	}
	st := tfsdk.State{Schema: sr.Schema, Raw: tftypes.NewValue(objType, attrs)}

	if d := st.SetAttribute(ctx, path.Root("id"), types.StringValue("ad-1")); d.HasError() {
		t.Fatalf("setting id: %v", d)
	}
	if d := st.SetAttribute(ctx, path.Root("name"), types.StringValue("monorepo")); d.HasError() {
		t.Fatalf("setting name: %v", d)
	}
	if d := st.SetAttribute(ctx, path.Root("propagate"), propagate); d.HasError() {
		t.Fatalf("setting propagate: %v", d)
	}

	resp := resource.UpdateResponse{State: st}
	r.Update(ctx, resource.UpdateRequest{Plan: tfsdk.Plan(st), State: st}, &resp)
	return resp
}

func TestUpdateSendsPropagateWhenTheConfigAsksForIt(t *testing.T) {
	f := &wiringFake{}
	r := newWiringResource(t, f)

	resp := runWiringUpdate(t, r, types.BoolValue(true))
	if resp.Diagnostics.HasError() {
		t.Fatalf("update: %v", resp.Diagnostics)
	}
	if f.top == nil {
		t.Fatal("no PATCH body was captured")
	}
	if f.top["propagate"] != true {
		t.Errorf("propagate must reach the wire as a request option, got top-level %v", f.top)
	}
}

func TestUpdateSendsNoOptionWhenPropagateIsFalse(t *testing.T) {
	// The default must not reach the workspaces the rule already created, and
	// `false` is the default. Asserted as ABSENT rather than as `false`: the
	// plain update path sends no options at all, and that is the path a
	// practitioner who never mentions `propagate` takes.
	f := &wiringFake{}
	r := newWiringResource(t, f)

	resp := runWiringUpdate(t, r, types.BoolValue(false))
	if resp.Diagnostics.HasError() {
		t.Fatalf("update: %v", resp.Diagnostics)
	}
	if _, ok := f.top["propagate"]; ok {
		t.Errorf("no propagation was asked for, so none belongs on the wire: %v", f.top)
	}
}

func TestUpdateSendsNoOptionWhenPropagateIsAbsent(t *testing.T) {
	// `propagate` is Optional and not Computed, so a configuration that never
	// mentions it arrives NULL rather than false. `ValueBool()` on a null Bool
	// is false, which is the behaviour this pins -- if it ever became Computed,
	// a null would start meaning "unknown" and this is what would notice.
	f := &wiringFake{}
	r := newWiringResource(t, f)

	resp := runWiringUpdate(t, r, types.BoolNull())
	if resp.Diagnostics.HasError() {
		t.Fatalf("update: %v", resp.Diagnostics)
	}
	if _, ok := f.top["propagate"]; ok {
		t.Errorf("an absent propagate must send nothing: %v", f.top)
	}
}
