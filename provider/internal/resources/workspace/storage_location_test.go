package workspace

import (
	"context"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/tfsdk"
	"github.com/hashicorp/terraform-plugin-go/tftypes"
	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// `storage_location` is create-only: the API refuses an update that changes it,
// because nothing moves a workspace's existing objects between buckets. The
// provider refuses at PLAN time instead of letting the apply 422, and instead of
// modelling it as RequiresReplace -- which would propose destroying a workspace
// holding live state for someone whose intent was to move data.

// runStorageModifyPlan drives ModifyPlan with a config and a state value for
// `storage_location`. A nil pointer means the attribute is null there.
func runStorageModifyPlan(t *testing.T, cfg, state *string) resource.ModifyPlanResponse {
	t.Helper()
	s := wsSchema(t)
	str := func(v *string) tftypes.Value {
		if v == nil {
			return tftypes.NewValue(tftypes.String, nil)
		}
		return tftypes.NewValue(tftypes.String, *v)
	}
	mk := func(v *string, withID bool) tftypes.Value {
		m := map[string]tftypes.Value{
			"name":             tftypes.NewValue(tftypes.String, "argocd"),
			"storage_location": str(v),
		}
		if withID {
			m["id"] = tftypes.NewValue(tftypes.String, "ws-1")
		}
		return wsValue(t, s, m)
	}
	stateVal, cfgVal := mk(state, true), mk(cfg, false)
	var r workspaceResource
	req := resource.ModifyPlanRequest{
		State:  tfsdk.State{Schema: s, Raw: stateVal},
		Config: tfsdk.Config{Schema: s, Raw: cfgVal},
		Plan:   tfsdk.Plan{Schema: s, Raw: mk(cfg, true)},
	}
	resp := resource.ModifyPlanResponse{Plan: tfsdk.Plan{Schema: s, Raw: mk(cfg, true)}}
	r.ModifyPlan(context.Background(), req, &resp)
	return resp
}

func errorsOf(resp resource.ModifyPlanResponse) []string {
	var out []string
	for _, d := range resp.Diagnostics.Errors() {
		out = append(out, d.Summary()+" :: "+d.Detail())
	}
	return out
}

// `ptr` is taken in this package (for *bool), hence the name.
func sptr(s string) *string { return &s }

func TestStorageLocationChangeIsRefusedAtPlanTime(t *testing.T) {
	resp := runStorageModifyPlan(t, sptr("cold"), sptr("archive"))
	errs := errorsOf(resp)
	if len(errs) != 1 {
		t.Fatalf("want exactly 1 error, got %d: %v", len(errs), errs)
	}
	// Both locations named, so the operator can see what it is and what it was
	// without reading state -- and the remedy spelled out, because the only way
	// back is to restore the old value.
	for _, want := range []string{"cannot be changed", "archive", "cold", "create a new workspace"} {
		if !strings.Contains(errs[0], want) {
			t.Errorf("error does not mention %q: %s", want, errs[0])
		}
	}
}

func TestStorageLocationUnchangedPlansCleanly(t *testing.T) {
	if errs := errorsOf(runStorageModifyPlan(t, sptr("archive"), sptr("archive"))); len(errs) != 0 {
		t.Errorf("unchanged location must not error: %v", errs)
	}
}

// The case most easily got wrong. Dropping the attribute from the configuration
// is NOT a request to change it -- it means the location is no longer managed
// here. Refusing that would block every plan for such a workspace, which is the
// shape the `agent_pool_id` warning exists for rather than an error.
func TestRemovingStorageLocationFromConfigIsNotRefused(t *testing.T) {
	if errs := errorsOf(runStorageModifyPlan(t, nil, sptr("archive"))); len(errs) != 0 {
		t.Errorf("an unmanaged location must not error: %v", errs)
	}
}

// Adopting a workspace whose location the config now names, where state has
// none, must not be read as a change either.
func TestSettingStorageLocationWhereStateHasNoneIsNotRefused(t *testing.T) {
	if errs := errorsOf(runStorageModifyPlan(t, sptr("archive"), nil)); len(errs) != 0 {
		t.Errorf("state-null must not error: %v", errs)
	}
}

// Null for empty, not StringValue(""). The attribute is Optional and not
// Computed, so a config omitting it plans null; writing "" into state is the
// "provider produced inconsistent result after apply" shape.
func TestStorageLocationReadsNullWhenTheServerReportsNothing(t *testing.T) {
	for _, tc := range []struct {
		name, server string
		wantNull     bool
	}{
		{"unset", "", true},
		{"set", "archive", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var m workspaceModel
			if d := readWorkspaceIntoModel(context.Background(), &terrapod.Workspace{StorageLocation: tc.server}, &m); d.HasError() {
				t.Fatalf("read: %v", d)
			}
			if m.StorageLocation.IsNull() != tc.wantNull {
				t.Errorf("IsNull() = %v, want %v (value %q)", m.StorageLocation.IsNull(), tc.wantNull, m.StorageLocation.ValueString())
			}
			if !tc.wantNull && m.StorageLocation.ValueString() != tc.server {
				t.Errorf("value = %q, want %q", m.StorageLocation.ValueString(), tc.server)
			}
		})
	}
}
