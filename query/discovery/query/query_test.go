package query

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/mattrobinsonsre/terrapod/query/discovery/tofu"
)

func TestRenderConfigFiltersAndArgs(t *testing.T) {
	q := Query{
		Type: "aws_vpcs",
		Filters: []Filter{
			{Name: "tag:env", Values: []string{"prod", "staging"}},
			{Name: "state", Values: []string{"available"}},
		},
		Args: map[string]string{
			"region": `"eu-west-1"`,
			"id":     `"vpc-123"`,
		},
	}
	got := q.RenderConfig()

	wantContains := []string{
		`data "aws_vpcs" "q" {`,
		"filter {",
		`name   = "tag:env"`,
		`values = ["prod", "staging"]`,
		`values = ["available"]`,
		// Args emitted in sorted order: id before region.
		"  id = \"vpc-123\"\n  region = \"eu-west-1\"\n",
		`output "terrapod_query_result" {`,
		"value = data.aws_vpcs.q",
	}
	for _, w := range wantContains {
		if !strings.Contains(got, w) {
			t.Errorf("RenderConfig() missing %q in:\n%s", w, got)
		}
	}
}

func TestRenderConfigEscapesStrings(t *testing.T) {
	q := Query{Type: "aws_instances", Filters: []Filter{{Name: `tag:Na"me`, Values: []string{`a"b`}}}}
	got := q.RenderConfig()
	if !strings.Contains(got, `name   = "tag:Na\"me"`) || !strings.Contains(got, `values = ["a\"b"]`) {
		t.Errorf("quotes not escaped in HCL:\n%s", got)
	}
}

// fakeTofu writes a stub `tofu` executable that emulates init/apply/output for
// hermetic executor tests — no real tofu, no provider download, no cloud.
func fakeTofu(t *testing.T, outputJSON string, failApply bool) string {
	t.Helper()
	dir := t.TempDir()
	outFile := filepath.Join(dir, "out.json")
	if err := os.WriteFile(outFile, []byte(outputJSON), 0o600); err != nil {
		t.Fatal(err)
	}
	failFlag := ""
	if failApply {
		failFlag = "1"
	}
	script := "#!/bin/sh\n" +
		"case \"$1\" in\n" +
		"  init) exit 0 ;;\n" +
		"  apply) if [ -n \"" + failFlag + "\" ]; then echo 'data source matched nothing' >&2; exit 1; fi; exit 0 ;;\n" +
		"  output) cat " + outFile + " ;;\n" +
		"  *) exit 0 ;;\n" +
		"esac\n"
	bin := filepath.Join(dir, "tofu")
	if err := os.WriteFile(bin, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	return bin
}

func newExecutor(t *testing.T, bin string) *Executor {
	t.Helper()
	work := t.TempDir()
	return &Executor{Runner: &tofu.Runner{Bin: bin, Dir: work}}
}

func TestRunReturnsStructuredResult(t *testing.T) {
	out := `{"ids":["vpc-0a","vpc-0b"],"tags":{"env":"prod"}}`
	e := newExecutor(t, fakeTofu(t, out, false))

	res, err := e.Run(context.Background(), `provider "aws" {}`, Query{
		Type:    "aws_vpcs",
		Filters: []Filter{{Name: "tag:env", Values: []string{"prod"}}},
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if res.Type != "aws_vpcs" || res.Name != "q" {
		t.Errorf("result identity = %s.%s", res.Type, res.Name)
	}
	if res.Empty {
		t.Error("result should not be empty (2 ids)")
	}
	var v map[string]any
	if err := json.Unmarshal(res.Value, &v); err != nil {
		t.Fatalf("value not JSON: %v", err)
	}
	ids, _ := v["ids"].([]any)
	if len(ids) != 2 {
		t.Errorf("expected 2 ids, got %v", v["ids"])
	}

	// The executor actually wrote the config files it ran.
	for _, f := range []string{"providers.tf", "query.tf"} {
		if _, err := os.Stat(filepath.Join(e.Runner.Dir, f)); err != nil {
			t.Errorf("expected %s written: %v", f, err)
		}
	}
}

func TestRunDetectsEmptyPluralResult(t *testing.T) {
	e := newExecutor(t, fakeTofu(t, `{"ids":[]}`, false))
	res, err := e.Run(context.Background(), "", Query{Type: "aws_subnets"})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if !res.Empty {
		t.Error("empty ids should set Empty=true")
	}
}

func TestRunSurfacesApplyError(t *testing.T) {
	e := newExecutor(t, fakeTofu(t, "{}", true))
	_, err := e.Run(context.Background(), "", Query{Type: "aws_vpc"})
	if err == nil {
		t.Fatal("expected apply error to surface")
	}
	if !strings.Contains(err.Error(), "apply query") {
		t.Errorf("error should be wrapped as apply query: %v", err)
	}
}
