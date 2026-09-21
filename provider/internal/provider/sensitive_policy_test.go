package provider

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/provider"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	rschema "github.com/hashicorp/terraform-plugin-framework/resource/schema"
)

// MUST_BE_SENSITIVE names every attribute that carries, or can carry, secret
// material — with the reason, so removing one is an argument rather than an
// edit (GHSA-9646-883f-wjjm).
//
// This is a POLICY assertion, and it exists because the schema golden next door
// cannot be one. A golden freezes the current value of `sensitive=` and so
// detects a CHANGE; it cannot flag an omission that was already there when the
// golden was written — and `schema.golden` duly recorded these as
// `sensitive=false` for two releases without anything objecting.
var mustBeSensitive = map[string]string{
	"terrapod_variable.value":                     "the variable's value; the whole point of the resource",
	"terrapod_variable_set_variable.value":        "same, on a variable set",
	"terrapod_provider_template.body":             "HCL rendered into a provider block — the canonical home of cloud credentials",
	"terrapod_provider_template.parameters_json":  "a parameter declared sensitive WITH a default puts that default here",
	"terrapod_catalog_item.variable_options_json": "same JSON-string shape; may carry a sensitive default",
	"terrapod_catalog_instance.input_values":      "the server omits sensitive inputs, so the provider is their sole durable holder",
	"terrapod_execution_hook.script":              "a shell body round-tripped into state; the description asks for no secrets, which is not enforcement",
	"terrapod_user.password":                      "a password",
	"terrapod_run_task.hmac_key":                  "forging a task result bypasses a mandatory run task's apply gate",
	"terrapod_notification_configuration.token":   "a delivery credential",
	"terrapod_agent_pool_token.token":             "a pool join token",
	"terrapod_gpg_key.ascii_armor":                "a signing key",

	// Nested, and invisible to this gate until it learned to recurse.
	"terrapod_autodiscovery_rule.run_task_templates[].hmac_key": "the same key as terrapod_run_task.hmac_key, supplied through a rule",
}

func TestEverySecretBearingAttributeIsMarkedSensitive(t *testing.T) {
	ctx := context.Background()
	p := New("test")()

	actual := map[string]bool{}
	for _, factory := range p.Resources(ctx) {
		r := factory()
		var md resource.MetadataResponse
		r.Metadata(ctx, resource.MetadataRequest{ProviderTypeName: providerTypeName}, &md)
		var sr resource.SchemaResponse
		r.Schema(ctx, resource.SchemaRequest{}, &sr)
		for name, a := range sr.Schema.Attributes {
			actual[md.TypeName+"."+name] = a.IsSensitive()
			// Recurse. Walking only the top level left every nested secret
			// invisible to this gate AND to the golden next door, which records
			// a nested attribute's TYPE but not its sensitive flag — so a
			// nested `Sensitive: true` could be deleted with nothing failing.
			collectNested(actual, md.TypeName+"."+name, a)
		}
	}
	for _, factory := range p.DataSources(ctx) {
		d := factory()
		var md datasource.MetadataResponse
		d.Metadata(ctx, datasource.MetadataRequest{ProviderTypeName: providerTypeName}, &md)
		var sr datasource.SchemaResponse
		d.Schema(ctx, datasource.SchemaRequest{}, &sr)
		for name, a := range sr.Schema.Attributes {
			actual[md.TypeName+"."+name] = a.IsSensitive()
		}
	}

	var unmarked, missing []string
	for attr, why := range mustBeSensitive {
		sensitive, present := actual[attr]
		if !present {
			missing = append(missing, attr)
			continue
		}
		if !sensitive {
			unmarked = append(unmarked, fmt.Sprintf("%s — %s", attr, why))
		}
	}
	sort.Strings(unmarked)
	sort.Strings(missing)

	if len(unmarked) > 0 {
		t.Errorf(
			"these attributes hold secret material and are not marked Sensitive, so they "+
				"land in plaintext state and print verbatim in plan output:\n  %s",
			strings.Join(unmarked, "\n  "),
		)
	}
	if len(missing) > 0 {
		t.Errorf(
			"these are named in the policy but no longer exist as top-level attributes. "+
				"Remove them, or the policy is describing a surface that is gone:\n  %s",
			strings.Join(missing, "\n  "),
		)
	}
}

// The provider's own token must never reach resource state — it is held in
// memory and sourced from the attribute or TERRAPOD_TOKEN.
func TestTheProviderTokenIsSensitive(t *testing.T) {
	ctx := context.Background()
	var sr provider.SchemaResponse
	New("test")().Schema(ctx, provider.SchemaRequest{}, &sr)

	a, ok := sr.Schema.Attributes["token"]
	if !ok {
		t.Fatal("the provider schema no longer declares a token attribute")
	}
	if !a.IsSensitive() {
		t.Error("provider.token is not Sensitive")
	}
}

// collectNested walks a nested attribute's children. The framework's own
// NestedAttribute interface is internal, so this type-switches on the concrete
// public schema types instead — verbose, but it needs no unexported access and
// fails loudly if a new nested kind appears.
func collectNested(out map[string]bool, prefix string, a rschema.Attribute) {
	var attrs map[string]rschema.Attribute
	switch n := a.(type) {
	case rschema.ListNestedAttribute:
		attrs = n.NestedObject.Attributes
	case rschema.SetNestedAttribute:
		attrs = n.NestedObject.Attributes
	case rschema.MapNestedAttribute:
		attrs = n.NestedObject.Attributes
	case rschema.SingleNestedAttribute:
		attrs = n.Attributes
	default:
		return
	}
	for name, child := range attrs {
		key := prefix + "[]." + name
		out[key] = child.IsSensitive()
		collectNested(out, key, child)
	}
}
