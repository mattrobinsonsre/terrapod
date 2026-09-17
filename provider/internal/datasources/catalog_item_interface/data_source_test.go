package catalog_item_interface

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
)

// The interface arrives as `map[string]any` decoded from the registry, so the
// readers have to tolerate a key that is absent or of the wrong type rather
// than panicking the provider on a module whose metadata is unusual.
func TestTheReadersTolerateMissingAndMistypedKeys(t *testing.T) {
	m := map[string]any{"name": "vpc_id", "required": true, "type": 7}
	if str(m, "name") != "vpc_id" {
		t.Errorf("name: %q", str(m, "name"))
	}
	if str(m, "type") != "" || str(m, "absent") != "" {
		t.Errorf("a mistyped or missing key should read as empty, got %q / %q", str(m, "type"), str(m, "absent"))
	}
	if !flag(m, "required") || flag(m, "sensitive") {
		t.Errorf("flags: required=%v sensitive=%v", flag(m, "required"), flag(m, "sensitive"))
	}
}

// "No default" and "a default of empty string" are different things to whoever
// is filling the form, so they must not both render as "".
func TestDefaultValueSeparatesNoDefaultFromAnEmptyOne(t *testing.T) {
	if got := defaultValue(nil); !got.IsNull() {
		t.Errorf("no default should be null, got %v", got)
	}
	// The registry stores a default JSON-encoded already, so a string passes
	// straight through instead of being encoded a second time.
	if got := defaultValue(`"eu-west-1"`); got.ValueString() != `"eu-west-1"` {
		t.Errorf("string default: %v", got)
	}
	if got := defaultValue(map[string]any{"a": float64(1)}); got.ValueString() != `{"a":1}` {
		t.Errorf("object default: %v", got)
	}
	if got := defaultValue(false); got.ValueString() != "false" {
		t.Errorf("bool default: %v", got)
	}
}

// A clean interface is null, not "" (#1707), so `interface_error != null` is
// the check a configuration can rely on.
func TestInterfaceErrorIsNullWhenTheInterfaceWasRead(t *testing.T) {
	if got := interfaceError(""); !got.IsNull() {
		t.Errorf("no reason should be null, got %v", got)
	}
	if got := interfaceError("main.tf: invalid HCL"); got.ValueString() != "main.tf: invalid HCL" {
		t.Errorf("reason: %v", got)
	}
}

func TestSchemaHasNoReservedProviderAttribute(t *testing.T) {
	resp := &datasource.SchemaResponse{}
	NewDataSource().Schema(context.Background(), datasource.SchemaRequest{}, resp)
	if resp.Diagnostics.HasError() {
		t.Fatal(resp.Diagnostics)
	}
	if _, ok := resp.Schema.Attributes["provider"]; ok {
		t.Error(`"provider" is a reserved root attribute name`)
	}
	for _, want := range []string{"catalog_item_id", "resolved_version", "inputs", "outputs", "interface_error"} {
		if _, ok := resp.Schema.Attributes[want]; !ok {
			t.Errorf("missing %s", want)
		}
	}
	inputs, ok := resp.Schema.Attributes["inputs"].(schema.ListNestedAttribute)
	if !ok {
		t.Fatal("inputs is not a list of nested objects")
	}
	for _, want := range []string{"name", "type", "description", "default", "required", "sensitive"} {
		if _, ok := inputs.NestedObject.Attributes[want]; !ok {
			t.Errorf("missing inputs.%s", want)
		}
	}
}
