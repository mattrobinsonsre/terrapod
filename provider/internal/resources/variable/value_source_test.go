package variable

import (
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// A vault-sourced variable is always sensitive, and the Read path skips Value
// for sensitive variables — so without an exception the reference would never
// be read back. That is perpetual plan drift: a reference edited in Terraform
// looks permanently unapplied, and one edited elsewhere is never detected.
func TestAVaultReferenceIsReadBackDespiteBeingSensitive(t *testing.T) {
	var m variableModel
	readVariableIntoModel(&terrapod.Variable{
		ID:          "var-1",
		Key:         "NETBOX_TOKEN",
		Category:    "env",
		Sensitive:   true,
		ValueSource: "vault",
		Value:       `{"mount":"secret","path":"apps/netbox","field":"apitoken"}`,
	}, &m)

	if m.ValueSource.ValueString() != "vault" {
		t.Fatalf("value_source lost: %q", m.ValueSource.ValueString())
	}
	if m.Value.ValueString() == "" {
		t.Fatal("the reference was skipped as if it were a secret — this is plan drift")
	}
}

func TestAnOrdinarySensitiveValueIsStillNotReadBack(t *testing.T) {
	// The exception above must not widen: a real secret still never round-trips
	// through Read, because the server returns nothing for it.
	m := variableModel{Value: types.StringValue("configured-locally")}
	readVariableIntoModel(&terrapod.Variable{
		ID: "var-1", Key: "SECRET", Category: "env", Sensitive: true, Value: "",
	}, &m)

	if m.Value.ValueString() != "configured-locally" {
		t.Errorf("the configured value was overwritten from a redacted read: %q", m.Value.ValueString())
	}
	if m.ValueSource.ValueString() != "static" {
		t.Errorf("a server sending no value-source must read as static, got %q", m.ValueSource.ValueString())
	}
}

func TestTheSourceReachesTheCreateAndUpdateRequests(t *testing.T) {
	// Well-formed conversions are worth nothing if nothing calls them.
	m := &variableModel{
		Key:         types.StringValue("K"),
		Category:    types.StringValue("env"),
		ValueSource: types.StringValue("vault"),
		Value:       types.StringValue(`{"mount":"m","path":"p","field":"f"}`),
	}
	if got := buildCreateVariableRequest(m).ValueSource; got != "vault" {
		t.Errorf("create dropped the source: %q", got)
	}
	up := buildUpdateVariableRequest(m)
	if up.ValueSource == nil || *up.ValueSource != "vault" {
		t.Errorf("update dropped the source: %v", up.ValueSource)
	}
}
