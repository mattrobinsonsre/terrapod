package variable_set

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/tfsdk"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-go/tftypes"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// fakeServer holds one variable set whose rule carries an exclusion, records the
// requests it receives, and answers a PATCH with the body it was sent.
type fakeServer struct {
	mu       sync.Mutex
	getCode  int
	requests []string
	patched  map[string]any
}

func (f *fakeServer) handler(stored map[string]any) http.HandlerFunc {
	reply := func(w http.ResponseWriter, rule any) {
		_ = json.NewEncoder(w).Encode(map[string]any{"data": map[string]any{
			"id": "varset-1", "type": "varsets",
			"attributes": map[string]any{"name": "vs", "assignment-rule": rule},
		}})
	}
	return func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		f.requests = append(f.requests, r.Method)
		switch r.Method {
		case http.MethodGet:
			if f.getCode != 0 {
				w.WriteHeader(f.getCode)
				return
			}
			reply(w, stored)
		case http.MethodPatch:
			raw, _ := io.ReadAll(r.Body)
			var body struct {
				Data struct {
					Attributes map[string]any `json:"attributes"`
				} `json:"data"`
			}
			_ = json.Unmarshal(raw, &body)
			f.patched = body.Data.Attributes
			reply(w, f.patched["assignment-rule"])
		default:
			w.WriteHeader(http.StatusMethodNotAllowed)
		}
	}
}

func newUpdateResource(t *testing.T, f *fakeServer, stored map[string]any) *variableSetResource {
	t.Helper()
	srv := httptest.NewServer(f.handler(stored))
	t.Cleanup(srv.Close)
	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: srv.URL, Token: "test-token"})
	if err != nil {
		t.Fatalf("building a client against the fake: %v", err)
	}
	return &variableSetResource{tc: tc}
}

func modelState(t *testing.T, m *variableSetModel) tfsdk.State {
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
	if d := st.Set(ctx, m); d.HasError() {
		t.Fatalf("building state: %v", d)
	}
	return st
}

func runUpdate(t *testing.T, r *variableSetResource, rule *assignmentRuleModel) resource.UpdateResponse {
	t.Helper()
	ctx := context.Background()
	m := &variableSetModel{
		ID:             types.StringValue("varset-1"),
		Name:           types.StringValue("vs"),
		Description:    types.StringValue(""),
		Global:         types.BoolValue(false),
		Priority:       types.BoolValue(false),
		AssignmentRule: rule,
		VarCount:       types.Int64Value(0),
		WorkspaceCount: types.Int64Value(0),
		CreatedAt:      types.StringValue(""),
		UpdatedAt:      types.StringValue(""),
	}
	st := modelState(t, m)
	plan := tfsdk.Plan(st)
	resp := resource.UpdateResponse{State: st}
	r.Update(ctx, resource.UpdateRequest{Plan: plan, State: st}, &resp)
	return resp
}

var storedWithExclusion = map[string]any{
	"labels":  map[string]any{"env": "dev"},
	"exclude": []any{map[string]any{"labels": map[string]any{"tier": "frozen"}}},
}

func TestUpdateSendsTheServersExclusionUnchanged(t *testing.T) {
	f := &fakeServer{}
	r := newUpdateResource(t, f, storedWithExclusion)

	resp := runUpdate(t, r, &assignmentRuleModel{Labels: map[string]string{"env": "prod"}})
	if resp.Diagnostics.HasError() {
		t.Fatalf("update: %v", resp.Diagnostics)
	}
	rule, _ := f.patched["assignment-rule"].(map[string]any)
	want, _ := json.Marshal(storedWithExclusion["exclude"])
	got, _ := json.Marshal(rule["exclude"])
	if string(got) != string(want) {
		t.Errorf("the PATCH did not carry the server's exclusion: %v", f.patched)
	}
	labels, _ := rule["labels"].(map[string]any)
	if labels["env"] != "prod" {
		t.Errorf("the managed part of the rule must come from config: %v", rule)
	}
}

func TestUpdateWithTheRuleBlockRemovedSendsNoRuleAndNoExclusion(t *testing.T) {
	f := &fakeServer{}
	r := newUpdateResource(t, f, storedWithExclusion)

	resp := runUpdate(t, r, nil)
	if resp.Diagnostics.HasError() {
		t.Fatalf("update: %v", resp.Diagnostics)
	}
	rule, present := f.patched["assignment-rule"]
	if !present || rule != nil {
		t.Errorf("removing the block must send an explicit null rule, got %v (present=%v)", rule, present)
	}
}

func TestAFailedReadStopsTheUpdateBeforeAnyWrite(t *testing.T) {
	// A definitive 4xx: a 5xx would be retried with backoff.
	f := &fakeServer{getCode: http.StatusForbidden}
	r := newUpdateResource(t, f, storedWithExclusion)

	resp := runUpdate(t, r, &assignmentRuleModel{Labels: map[string]string{"env": "prod"}})
	if !resp.Diagnostics.HasError() {
		t.Fatal("an unreadable current rule must fail the update")
	}
	for _, m := range f.requests {
		if m == http.MethodPatch {
			t.Errorf("a write was sent although the read failed: %v", f.requests)
		}
	}
}
