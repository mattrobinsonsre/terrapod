package provider

import (
	"context"
	"sort"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/provider"
)

// The provider's OWN configuration block is pinned by nothing else: the schema
// golden records `resource …` and `data …` lines only, so adding, renaming or
// retyping a provider attribute changes no committed file and fails no gate
// (#2036). Operators write these by hand in `provider "terrapod" {}`, so a
// rename is as breaking as a resource attribute's and deserves the same ratchet.
func providerSchemaAttrs(t *testing.T) map[string]provider.SchemaResponse {
	t.Helper()
	var resp provider.SchemaResponse
	New("test")().Schema(context.Background(), provider.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("provider schema: %v", resp.Diagnostics)
	}
	return map[string]provider.SchemaResponse{"": resp}
}

func TestTheProviderConfigurationBlockIsExactlyThese(t *testing.T) {
	resp := providerSchemaAttrs(t)[""]
	var got []string
	for name := range resp.Schema.Attributes {
		got = append(got, name)
	}
	sort.Strings(got)

	want := []string{"hostname", "skip_tls_verify", "token", "workspace"}
	if len(got) != len(want) {
		t.Fatalf("provider attributes changed: got %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("provider attributes changed: got %v, want %v", got, want)
			break
		}
	}
}

// Every one of them must be Optional. The provider is configured entirely from
// the environment inside a run -- the runner exports TERRAPOD_HOSTNAME,
// TERRAPOD_TOKEN and TERRAPOD_WORKSPACE -- so a Required attribute here would
// make `provider "terrapod" {}` invalid and break the declared-inventory story
// outright, for every workspace at once.
func TestEveryProviderAttributeIsOptional(t *testing.T) {
	resp := providerSchemaAttrs(t)[""]
	for name, a := range resp.Schema.Attributes {
		if a.IsRequired() {
			t.Errorf("%q is Required, so `provider \"terrapod\" {}` cannot be written; "+
				"configure it from the environment instead", name)
		}
	}
}

// `token` is the only secret, and it must stay marked. Asserted here as well as
// in the sensitive policy test because this file is the one that enumerates the
// block, so a newly added secret-bearing attribute is caught by whichever runs.
func TestOnlyTheTokenIsSensitive(t *testing.T) {
	resp := providerSchemaAttrs(t)[""]
	for name, a := range resp.Schema.Attributes {
		if sensitive := a.IsSensitive(); sensitive != (name == "token") {
			t.Errorf("%q sensitive=%v, expected %v", name, sensitive, name == "token")
		}
	}
}
