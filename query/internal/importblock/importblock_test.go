package importblock

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/mattrobinsonsre/terrapod/query/internal/query"
)

func result(t *testing.T, dsType, valueJSON string) *query.Result {
	t.Helper()
	return &query.Result{Type: dsType, Name: "q", Value: json.RawMessage(valueJSON)}
}

func TestPluralResultOneBlockPerID(t *testing.T) {
	res := result(t, "aws_vpcs", `{"ids":["vpc-0a1b","vpc-0c2d"],"tags":{}}`)
	blocks, err := FromResult(res, Options{})
	if err != nil {
		t.Fatal(err)
	}
	if len(blocks) != 2 {
		t.Fatalf("expected 2 blocks, got %d", len(blocks))
	}
	// Resource type singularised from the data-source name.
	for _, b := range blocks {
		if !strings.HasPrefix(b.To, "aws_vpc.") {
			t.Errorf("expected aws_vpc.* address, got %q", b.To)
		}
	}
	// Names derive from the ids (dashes are legal in Terraform resource names).
	if blocks[0].To != "aws_vpc.vpc-0a1b" || blocks[0].ID != "vpc-0a1b" {
		t.Errorf("unexpected first block: %+v", blocks[0])
	}
}

func TestSingularResultSingleBlock(t *testing.T) {
	res := result(t, "aws_vpc", `{"id":"vpc-single","cidr_block":"10.0.0.0/16"}`)
	blocks, err := FromResult(res, Options{})
	if err != nil {
		t.Fatal(err)
	}
	if len(blocks) != 1 || blocks[0].ID != "vpc-single" || blocks[0].To != "aws_vpc.vpc-single" {
		t.Fatalf("unexpected blocks: %+v", blocks)
	}
}

func TestResourceTypeOverride(t *testing.T) {
	res := result(t, "aws_vpcs", `{"ids":["vpc-1"]}`)
	blocks, err := FromResult(res, Options{ResourceType: "aws_vpc_custom"})
	if err != nil {
		t.Fatal(err)
	}
	if blocks[0].To != "aws_vpc_custom.vpc-1" {
		t.Errorf("override ignored: %q", blocks[0].To)
	}
}

func TestEmptyResultNoBlocks(t *testing.T) {
	blocks, err := FromResult(result(t, "aws_subnets", `{"ids":[]}`), Options{})
	if err != nil {
		t.Fatal(err)
	}
	if len(blocks) != 0 {
		t.Errorf("empty ids should yield no blocks, got %d", len(blocks))
	}
}

func TestNameSanitizationAndUniqueness(t *testing.T) {
	// An id starting with a digit and containing an illegal char, plus a
	// collision after sanitisation.
	res := result(t, "custom_things", `{"ids":["9weird.id","9weird.id"]}`)
	blocks, err := FromResult(res, Options{ResourceType: "custom_thing"})
	if err != nil {
		t.Fatal(err)
	}
	// '.' → '_', leading digit gets an r_ prefix.
	if blocks[0].To != "custom_thing.r_9weird_id" {
		t.Errorf("sanitisation wrong: %q", blocks[0].To)
	}
	// Second collision disambiguated.
	if blocks[1].To != "custom_thing.r_9weird_id_2" {
		t.Errorf("collision not disambiguated: %q", blocks[1].To)
	}
	// IDs are preserved verbatim even though names were sanitised.
	if blocks[0].ID != "9weird.id" || blocks[1].ID != "9weird.id" {
		t.Errorf("ids should be preserved verbatim: %+v", blocks)
	}
}

func TestRenderHCL(t *testing.T) {
	blocks := []Block{{To: "aws_vpc.vpc_a", ID: "vpc-a"}, {To: "aws_vpc.vpc_b", ID: "vpc-b"}}
	got := Render(blocks)
	want := "import {\n  to = aws_vpc.vpc_a\n  id = \"vpc-a\"\n}\n\nimport {\n  to = aws_vpc.vpc_b\n  id = \"vpc-b\"\n}\n"
	if got != want {
		t.Errorf("Render:\n%q\nwant:\n%q", got, want)
	}
}
