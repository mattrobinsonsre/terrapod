package provider

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// The provider never touches the TFE-compatibility surface (#2469).
//
// That surface exists for `terraform`, `tofu` and `tfci`. It is engine-filtered
// to Terraform and its serialiser strips every native-only attribute from its
// responses, so a provider reading a resource there cannot see the fields it
// just wrote: `terrapod_workspace` with any configure attribute set failed
// every apply with "Provider produced inconsistent result after apply" while
// the server held the values correctly.
//
// This was briefly a positional rule, permitting a TFE path as the legacy arm
// of a native-first pair so a 2.0 provider could still reach a 1.x server. No
// such window was ever promised -- `docs/versioning-and-support.md` covers
// API<->runner/listener, runner<->listener and runner<->execution environment,
// and says nothing about the provider or the SDK -- so the exception bought
// nothing and cost every call site the question of which surface answered. The
// provider targets the same major as the API, which is stated in that document,
// and the rule here is now simply zero.
//
// Zero is also why this is checkable at a glance rather than by parsing
// functions: there is nothing to attribute to a caller, so the gate cannot be
// wrong about which function a literal belongs to -- a thing its earlier,
// cleverer version could be.
var tfePath = regexp.MustCompile(`"/api/(?:tfe/)?v2[^"]*"`)

func TestTheProviderNeverPrefersTheTFESurface(t *testing.T) {
	root := providerModuleDir(t)
	var scanned int

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
		scanned++
		hits := tfePath.FindAllString(string(src), -1)
		if len(hits) == 0 {
			return nil
		}
		rel, _ := filepath.Rel(root, path)
		t.Errorf("%s reaches the TFE-compatibility surface (%d literal(s), e.g. %s).\n"+
			"That surface strips native-only attributes from its responses, so a resource "+
			"read there comes back missing fields the provider just wrote (#2469). Use the "+
			"native path, and reach it through a typed go-terrapod method rather than a raw "+
			"path where one exists.", rel, len(hits), hits[0])
		return nil
	})
	if err != nil {
		t.Fatalf("walking %s: %v", root, err)
	}

	// A floor, so a walk that stops finding the tree fails instead of passing on
	// nothing. Its earlier version earned this immediately: `providerRepoDir`
	// matched on the directory NAME and stopped at `provider/internal/provider`,
	// so the gate was checking one file and reporting success.
	if scanned < 50 {
		t.Fatalf("only %d non-test Go files walked; the walk is not finding the provider "+
			"module (looked under %s)", scanned, root)
	}
}

// providerModuleDir returns the provider module's root -- the directory holding
// go.mod -- found by walking up from the test's own working directory, which
// `go test` sets per package.
//
// By go.mod rather than by name: `provider/internal/provider` is also called
// "provider", so a name match finds the package directory first.
func providerModuleDir(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 8; i++ {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			return dir
		}
		dir = filepath.Dir(dir)
	}
	t.Fatalf("could not find the provider module root from %s", dir)
	return ""
}
