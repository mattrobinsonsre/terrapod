package catalog_instance

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/types"
	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// The engine choice on a catalog instance (#2372).
//
// The API accepts `execution-backend` and `engine-version` on BOTH provision
// and reconfigure, and the provider could set neither -- so an item whose
// `execution_backend_constraints` exclude the deployment's default backend
// could not be provisioned from Terraform at all.
//
// The two attributes are deliberately NOT symmetrical on the read leg, and
// that asymmetry is the thing worth pinning:
//
//   - the BACKEND is stored as sent, so reading it back is safe and is what
//     detects an out-of-band move onto another engine.
//   - the VERSION is not. `_select_runtime` resolves a partial like "1.12" to
//     an exact offered binary before storing it, so copying the server's
//     answer over a CONFIGURED value is "Provider produced inconsistent result
//     after apply" -- the v1.9.0 class. The configured value therefore stands,
//     and the resolved one is reported only when nothing was configured.
//
// The workspace resource reads its own engine_version straight back because
// the workspace API stores that string verbatim. This path differs, so it must
// not copy that shape; a test that only drove the backend would miss it.

func TestBothPathsSendTheEngineChoice(t *testing.T) {
	for _, tc := range []struct {
		name  string
		build func(*catalogInstanceModel) map[string]any
	}{
		{"provision", buildProvisionAttrs},
		{"reconfigure", buildReconfigureAttrs},
	} {
		t.Run(tc.name, func(t *testing.T) {
			m := catalogInstanceModel{
				Name:             types.StringValue("x"),
				AgentPoolID:      types.StringValue("apool-1"),
				ExecutionBackend: types.StringValue("terraform"),
				EngineVersion:    types.StringValue("1.9.8"),
			}
			attrs := tc.build(&m)
			if got, ok := attrs["execution-backend"]; !ok || got != "terraform" {
				t.Errorf("%s sent execution-backend = %#v (sent=%v), want terraform", tc.name, got, ok)
			}
			if got, ok := attrs["engine-version"]; !ok || got != "1.9.8" {
				t.Errorf("%s sent engine-version = %#v (sent=%v), want 1.9.8", tc.name, got, ok)
			}
		})
	}
}

func TestNeitherIsSentWhenNeitherIsConfigured(t *testing.T) {
	m := catalogInstanceModel{
		Name:        types.StringValue("x"),
		AgentPoolID: types.StringValue("apool-1"),
	}
	attrs := buildProvisionAttrs(&m)
	for _, k := range []string{"execution-backend", "engine-version"} {
		if _, sent := attrs[k]; sent {
			t.Errorf("%s was sent although nothing was configured; the deployment's default must stand", k)
		}
	}
}

func TestTheBackendIsReadBackSoDriftIsVisible(t *testing.T) {
	m := catalogInstanceModel{EngineVersion: types.StringNull()}
	inst := &terrapod.CatalogInstance{ID: "ws-c0ffee", Attributes: map[string]any{
		"name":              "x",
		"agent-pool-id":     "apool-1",
		"execution-backend": "terraform",
	}}
	if d := readCatalogInstanceIntoModel(context.Background(), inst, &m); d.HasError() {
		t.Fatalf("readCatalogInstanceIntoModel: %v", d)
	}
	if m.ExecutionBackend.ValueString() != "terraform" {
		t.Errorf("execution_backend read back as %#v, want terraform -- an out-of-band move must be visible",
			m.ExecutionBackend)
	}
}

func TestAConfiguredVersionSurvivesTheServerResolvingIt(t *testing.T) {
	// The practitioner asked for a partial version; the server stored the
	// exact binary it chose. Taking the server's answer here would make every
	// apply fail with "inconsistent result after apply".
	m := catalogInstanceModel{EngineVersion: types.StringValue("1.12")}
	inst := &terrapod.CatalogInstance{ID: "ws-c0ffee", Attributes: map[string]any{
		"name":           "x",
		"agent-pool-id":  "apool-1",
		"engine-version": "1.12.4",
	}}
	if d := readCatalogInstanceIntoModel(context.Background(), inst, &m); d.HasError() {
		t.Fatalf("readCatalogInstanceIntoModel: %v", d)
	}
	if m.EngineVersion.ValueString() != "1.12" {
		t.Errorf("engine_version read back as %#v, want the configured \"1.12\" -- overwriting it with the resolved value is an inconsistent-result error",
			m.EngineVersion)
	}
}

func TestAnUnconfiguredVersionReportsWhatTheServerResolved(t *testing.T) {
	m := catalogInstanceModel{EngineVersion: types.StringNull()}
	inst := &terrapod.CatalogInstance{ID: "ws-c0ffee", Attributes: map[string]any{
		"name":           "x",
		"agent-pool-id":  "apool-1",
		"engine-version": "1.12.4",
	}}
	if d := readCatalogInstanceIntoModel(context.Background(), inst, &m); d.HasError() {
		t.Fatalf("readCatalogInstanceIntoModel: %v", d)
	}
	if m.EngineVersion.ValueString() != "1.12.4" {
		t.Errorf("engine_version read back as %#v, want the resolved 1.12.4 when nothing was configured",
			m.EngineVersion)
	}
}
