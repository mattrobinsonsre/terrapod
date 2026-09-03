package variable_set_variable

import (
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// The "define once, apply to many" Vault case: a vault-sourced varset variable
// is always sensitive, and Read skips Value for sensitive variables — so
// without the exception the reference would never be read back, which is
// perpetual plan drift. Mirrors the workspace-variable behaviour (#1439).
func TestVSVVaultReferenceIsReadBackDespiteBeingSensitive(t *testing.T) {
	var m variableSetVariableModel
	readVSVFromSDK(&terrapod.VariableSetVariable{
		ID:          "var-1",
		Key:         "DB_PASSWORD",
		Category:    "env",
		Sensitive:   true,
		ValueSource: "vault",
		Value:       `{"mount":"secret","path":"apps/payments","field":"db_password"}`,
	}, &m)

	if m.ValueSource.ValueString() != "vault" {
		t.Fatalf("value_source lost: %q", m.ValueSource.ValueString())
	}
	if m.Value.ValueString() == "" {
		t.Fatal("the reference was skipped as if it were a secret — this is plan drift")
	}
}

func TestVSVOrdinarySensitiveValueIsStillNotReadBack(t *testing.T) {
	m := variableSetVariableModel{Value: types.StringValue("configured-locally")}
	readVSVFromSDK(&terrapod.VariableSetVariable{
		ID: "var-1", Key: "SECRET", Category: "env", Sensitive: true, Value: "",
	}, &m)

	if m.Value.ValueString() != "configured-locally" {
		t.Errorf("configured value overwritten from a redacted read: %q", m.Value.ValueString())
	}
	if m.ValueSource.ValueString() != "static" {
		t.Errorf("no value-source from the server must read as static, got %q", m.ValueSource.ValueString())
	}
}

func TestVSVSourceReachesTheCreateAndUpdateRequests(t *testing.T) {
	m := &variableSetVariableModel{
		Key:         types.StringValue("K"),
		Category:    types.StringValue("env"),
		ValueSource: types.StringValue("vault"),
		Value:       types.StringValue(`{"mount":"m","path":"p","field":"f"}`),
	}
	if got := buildCreateVSVRequest(m).ValueSource; got != "vault" {
		t.Errorf("create dropped the source: %q", got)
	}
	up := buildUpdateVSVRequest(m)
	if up.ValueSource == nil || *up.ValueSource != "vault" {
		t.Errorf("update dropped the source: %v", up.ValueSource)
	}
}
