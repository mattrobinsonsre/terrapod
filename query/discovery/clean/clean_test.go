package clean

import (
	"strings"
	"testing"

	"github.com/mattrobinsonsre/terrapod/query/discovery/schema"
)

// testSchema mirrors the shape of a real provider schema (an aws_vpc-like
// resource) with the attribute classes the cleanup pass keys off. Values/ids in
// the config fixtures below are synthetic — no real account or resource ids.
func testSchema() *schema.Schema {
	opt := schema.Attribute{Optional: true}
	req := schema.Attribute{Required: true}
	computedOnly := schema.Attribute{Computed: true}
	optComputed := schema.Attribute{Optional: true, Computed: true}
	return &schema.Schema{
		ProviderSchemas: map[string]schema.Provider{
			"registry.opentofu.org/hashicorp/aws": {
				ResourceSchemas: map[string]schema.ResourceSchema{
					"aws_vpc": {Block: schema.Block{
						Attributes: map[string]schema.Attribute{
							"cidr_block":          req,
							"enable_dns_support":  opt,
							"instance_tenancy":    optComputed,
							"ipv6_cidr_block":     opt,
							"ipv6_netmask_length": opt,
							"assign_ipv6":         opt,
							"ipam_pool_id":        opt,
							"arn":                 computedOnly,
							"tags_all":            optComputed,
						},
						BlockTypes: map[string]schema.BlockType{
							"timeouts": {NestingMode: "single", Block: schema.Block{
								Attributes: map[string]schema.Attribute{"create": opt},
							}},
						},
					}},
				},
			},
		},
	}
}

func TestClean_prunesZeroAndComputedOnly(t *testing.T) {
	// A generate-config-out-shaped block: required + non-zero optional kept;
	// every zero-valued optional (incl. the conflicting ipv6 pair) + the
	// computed-only `arn` dropped; the empty `timeouts {}` block removed.
	src := `resource "aws_vpc" "example" {
  arn                 = "arn:aws:ec2:region:acct:vpc/example"
  cidr_block          = "10.0.0.0/16"
  enable_dns_support  = true
  instance_tenancy    = "default"
  ipv6_cidr_block     = ""
  ipv6_netmask_length = 0
  assign_ipv6         = false
  ipam_pool_id        = null
  tags_all            = {}
  timeouts {}
}
`
	out, rep, err := Clean([]byte(src), testSchema())
	if err != nil {
		t.Fatalf("Clean: %v", err)
	}
	got := string(out)

	// Dropped: computed-only + every zero-valued optional + the empty block.
	for _, dropped := range []string{"arn", "ipv6_cidr_block", "ipv6_netmask_length", "assign_ipv6", "ipam_pool_id", "tags_all", "timeouts"} {
		if strings.Contains(got, dropped+" ") || strings.Contains(got, dropped+"\t") || strings.Contains(got, dropped+"{") {
			t.Errorf("expected %q to be pruned, still present:\n%s", dropped, got)
		}
	}
	// Kept: required + non-zero optional/optional-computed.
	for _, kept := range []string{"cidr_block", "enable_dns_support", "instance_tenancy"} {
		if !strings.Contains(got, kept) {
			t.Errorf("expected %q to be kept, missing:\n%s", kept, got)
		}
	}
	if rep.RemovedComputed != 1 {
		t.Errorf("RemovedComputed = %d, want 1 (arn)", rep.RemovedComputed)
	}
	// ipv6_cidr_block, ipv6_netmask_length, assign_ipv6, ipam_pool_id, tags_all
	if rep.RemovedZero != 5 {
		t.Errorf("RemovedZero = %d, want 5", rep.RemovedZero)
	}
	if rep.Resources != 1 {
		t.Errorf("Resources = %d, want 1", rep.Resources)
	}
}

func TestClean_keepsRequiredEvenIfZero(t *testing.T) {
	// A required argument at its zero value is still kept — removing it would
	// make the config invalid; better a config a human must eyeball.
	src := `resource "aws_vpc" "example" {
  cidr_block = ""
}
`
	out, _, err := Clean([]byte(src), testSchema())
	if err != nil {
		t.Fatalf("Clean: %v", err)
	}
	if !strings.Contains(string(out), "cidr_block") {
		t.Errorf("required cidr_block was pruned:\n%s", out)
	}
}

func TestClean_leavesUnknownResourceTypesUntouched(t *testing.T) {
	// A resource type absent from the schema cannot be classified, so it is left
	// verbatim and reported — never silently stripped.
	src := `resource "unknown_thing" "x" {
  whatever = ""
}
`
	out, rep, err := Clean([]byte(src), testSchema())
	if err != nil {
		t.Fatalf("Clean: %v", err)
	}
	if !strings.Contains(string(out), "whatever") {
		t.Errorf("unknown-type attribute was pruned:\n%s", out)
	}
	if len(rep.UnknownTypes) != 1 || rep.UnknownTypes[0] != "unknown_thing" {
		t.Errorf("UnknownTypes = %v, want [unknown_thing]", rep.UnknownTypes)
	}
}

func TestClean_keepsExpressionsItCannotEvaluate(t *testing.T) {
	// A value with a reference (not a literal) can't be evaluated to a zero
	// check, so it is conservatively kept.
	src := `resource "aws_vpc" "example" {
  cidr_block         = "10.0.0.0/16"
  enable_dns_support = var.enable
}
`
	out, _, err := Clean([]byte(src), testSchema())
	if err != nil {
		t.Fatalf("Clean: %v", err)
	}
	if !strings.Contains(string(out), "enable_dns_support") {
		t.Errorf("non-literal attribute was pruned:\n%s", out)
	}
}
