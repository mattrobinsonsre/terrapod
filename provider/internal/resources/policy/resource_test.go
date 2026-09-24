package policy

import (
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// #1765. A policy has no GET of its own, so its lifecycle leans on two things
// that are easy to get subtly wrong: the composite import id, and keeping the
// set id in whatever form the configuration wrote.

func TestImportIDSplitsOnTheLastSeparator(t *testing.T) {
	set, pol, ok := splitImportID("polset-aaa/pol-bbb")
	if !ok || set != "polset-aaa" || pol != "pol-bbb" {
		t.Fatalf("got (%q, %q, %v)", set, pol, ok)
	}
}

func TestImportIDNeedsBothHalves(t *testing.T) {
	// A policy cannot be read without its set, so a bare id must be refused
	// with an explanation rather than half-importing into a broken state.
	for _, bad := range []string{"pol-bbb", "", "/pol-bbb", "polset-aaa/"} {
		if _, _, ok := splitImportID(bad); ok {
			t.Fatalf("%q should not parse as an import id", bad)
		}
	}
}

func TestReadKeepsTheSetIDFormTheConfigWrote(t *testing.T) {
	// The relationship carries the prefixed id; a configuration interpolating
	// `terrapod_policy_set.x.id` wrote that form too, but one written bare
	// must not be rewritten under the practitioner (#1748).
	m := &policyModel{PolicySetID: types.StringValue("aaa")}
	readIntoModel(&terrapod.Policy{ID: "pol-1", PolicySetID: "polset-aaa"}, m)

	if got := m.PolicySetID.ValueString(); got != "aaa" {
		t.Fatalf("policy_set_id = %q, want the configured bare form", got)
	}
}

func TestReadFollowsTheServerWhenTheSetGenuinelyDiffers(t *testing.T) {
	m := &policyModel{PolicySetID: types.StringValue("polset-aaa")}
	readIntoModel(&terrapod.Policy{ID: "pol-1", PolicySetID: "polset-zzz"}, m)

	if got := m.PolicySetID.ValueString(); got != "polset-zzz" {
		t.Fatalf("policy_set_id = %q, want the server's", got)
	}
}

func TestAnUnsetDescriptionStaysNull(t *testing.T) {
	// The server answers an absent optional with "". Storing it makes every
	// later plan show null -> "" and never converge.
	m := &policyModel{Description: types.StringNull()}
	readIntoModel(&terrapod.Policy{ID: "pol-1", Name: "n", Rego: "package terrapod"}, m)

	if !m.Description.IsNull() {
		t.Fatalf("description = %q, want null", m.Description.ValueString())
	}
}

func TestADescriptionSetOutsideTerraformIsStillSeen(t *testing.T) {
	m := &policyModel{Description: types.StringNull()}
	readIntoModel(&terrapod.Policy{ID: "pol-1", Description: "set in the UI"}, m)

	if m.Description.ValueString() != "set in the UI" {
		t.Fatalf("description = %q, want the server's value", m.Description.ValueString())
	}
}

func TestRegoAndTimestampsRoundTrip(t *testing.T) {
	m := &policyModel{}
	readIntoModel(&terrapod.Policy{
		ID:        "pol-1",
		Name:      "no-public-buckets",
		Rego:      "package terrapod\n\ndeny contains msg if { false }",
		CreatedAt: "2026-09-22T00:00:00Z",
		UpdatedAt: "2026-09-22T01:00:00Z",
	}, m)

	if m.Name.ValueString() != "no-public-buckets" {
		t.Fatalf("name = %q", m.Name.ValueString())
	}
	if m.Rego.IsNull() || m.Rego.ValueString() == "" {
		t.Fatal("rego must round-trip; it is the whole resource")
	}
	if m.CreatedAt.ValueString() != "2026-09-22T00:00:00Z" {
		t.Fatalf("created_at = %q", m.CreatedAt.ValueString())
	}
}
