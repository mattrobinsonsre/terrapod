package provider

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// The provider must not PREFER the TFE-compatibility surface (#2469).
//
// That surface exists for `terraform`, `tofu` and `tfci`. It is engine-filtered
// to Terraform and its serialiser strips every native-only attribute from its
// responses (`_native_only` in `api/routers/tfe_v2.py`), so a provider reading a
// resource there cannot see the fields it just wrote: `terrapod_workspace` with
// any configure attribute set failed every apply with "Provider produced
// inconsistent result after apply" while the server held the values correctly.
//
// It is NOT a ban on the literal. A supported 1.x server has no `/api/v1` mount
// at all, so some resources can only be reached there, and the TFE path is the
// legacy arm of a native-first pair. What this refuses is a TFE path used on its
// own -- which is indistinguishable, at the call site, from the bug above.
//
// So the rule is positional: every TFE literal must sit in a function that also
// reaches a native path and checks for the fallback status. The web UI has the
// same gate for the same reason (`web/tests/engine-scoped-prefixes.test.ts`).
var (
	tfeLiteral    = regexp.MustCompile(`"/api/(?:tfe/)?v2/`)
	nativeLiteral = regexp.MustCompile(`"/api/(?:terrapod/)?v1/`)
)

func TestTheProviderNeverPrefersTheTFESurface(t *testing.T) {
	root := providerRepoDir(t)
	var checked, withLiteral int

	err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() || !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		src, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		checked++
		if !tfeLiteral.Match(src) {
			return nil
		}
		withLiteral++
		rel, _ := filepath.Rel(root, path)
		for _, fn := range splitFuncs(string(src)) {
			if !tfeLiteral.MatchString(fn.body) {
				continue
			}
			// The legacy arm of a native-first pair: the same function reaches a
			// native path and only falls back on the documented status.
			if !nativeLiteral.MatchString(fn.body) {
				t.Errorf("%s: %s uses the TFE surface with no native path beside it.\n"+
					"The TFE route strips native-only attributes from its responses, so a "+
					"resource read there comes back missing fields the provider just wrote "+
					"(#2469). Read natively, and keep the TFE path only as the fallback arm "+
					"for a server with no /api/v1 mount.", rel, fn.name)
				continue
			}
			if !strings.Contains(fn.body, "StatusMethodNotAllowed") {
				t.Errorf("%s: %s reaches both surfaces but never checks for the fallback "+
					"status, so which one answers is not decided anywhere. Fall back only on "+
					"405 -- a 404 is the answer, and re-asking the Terraform-only surface "+
					"could find nothing, or the wrong thing.", rel, fn.name)
			}
		}
		return nil
	})
	if err != nil {
		t.Fatalf("walking %s: %v", root, err)
	}

	// Floors, so a walk that stops matching fails instead of passing on nothing.
	if checked < 50 {
		t.Fatalf("only %d non-test Go files walked; the walk is not finding the provider", checked)
	}
	if withLiteral < 1 {
		t.Fatalf("no file carries a TFE literal -- if that is genuinely true now, this gate " +
			"should assert zero outright rather than silently check nothing")
	}
}

type goFunc struct {
	name string
	body string
}

// splitFuncs cuts a file at top-level `func` declarations. Crude on purpose: it
// only has to attribute a literal to the function containing it, and a brace
// matcher would be a Go parser nobody needs here.
func splitFuncs(src string) []goFunc {
	var out []goFunc
	lines := strings.Split(src, "\n")
	name, body := "", []string{}
	flush := func() {
		if name != "" {
			out = append(out, goFunc{name: name, body: strings.Join(body, "\n")})
		}
	}
	decl := regexp.MustCompile(`^func (?:\([^)]*\) )?(\w+)`)
	for _, l := range lines {
		if m := decl.FindStringSubmatch(l); m != nil {
			flush()
			name, body = m[1], []string{l}
			continue
		}
		if name != "" {
			body = append(body, l)
		}
	}
	flush()
	return out
}

// providerRepoDir returns the `provider/` directory, found by walking up from
// this test file rather than from the working directory, which `go test`
// changes per package.
func providerRepoDir(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	// Walk up to the module root -- the directory holding go.mod. Matching on
	// the directory NAME finds `provider/internal/provider` first, which is one
	// file and would make this gate check almost nothing.
	for i := 0; i < 8; i++ {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			return dir
		}
		dir = filepath.Dir(dir)
	}
	t.Fatalf("could not find the provider directory from %s", dir)
	return ""
}
