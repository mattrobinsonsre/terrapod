package terrapod

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// Nothing the Terraform provider reaches may touch the TFE-compatibility
// surface at all (#2469).
//
// That surface exists for `terraform`, `tofu` and `tfci`. It is engine-filtered
// to Terraform and its serialiser strips every native-only attribute from its
// responses, so a consumer reading a resource there cannot see the fields it
// just wrote -- which is how `terrapod_workspace` came to fail every apply that
// set a configure attribute while the server stored the values correctly.
//
// This started as a positional rule, allowing a TFE path as the legacy arm of a
// native-first pair so that a 2.0 provider could still talk to a 1.x server.
// That window was never promised: `docs/versioning-and-support.md` gives a skew
// window for API<->runner/listener, runner<->listener and runner<->execution
// environment, and says nothing about the provider or this SDK;
// `docs/migration.md` names no target version either. So the fallback was
// protecting an obligation that did not exist, at the cost of every call site
// having to reason about which surface answered and what each one strips. The
// provider and this SDK target the same major as the API, and the rule is now
// the simple one: no TFE paths, except in the files the provider cannot reach.
var tfePath = regexp.MustCompile(`"/api/(?:tfe/)?v2[^"]*"`)

// notProviderFacing are the files whose TFE calls the Terraform provider does
// not reach, with what each is waiting on. Measured rather than assumed: the
// provider has zero call sites into any of them.
//
// They are exempt rather than fixed because moving them has a cost and no
// caller asking for it yet -- and because an exemption that states what is
// missing is reviewable, where silence is not. A file leaves this list by
// moving its calls, not by being forgotten: TestTheExemptionsAreStillAccurate
// fails an entry whose file no longer calls that surface.
var notProviderFacing = map[string]string{
	"runs.go": "the run collection and a workspace's runs have native routes; the run " +
		"queue and the plan/apply log paths do not. Read by the web UI and migrate, " +
		"neither of which is a provider.",
	"state_versions.go": "state-version CONTENT has no native route at all " +
		"(`/api/v1/state-versions/{id}/download` is a different endpoint). Read by " +
		"migrate on the write side of a migration.",
	"policy_checks.go":  "no native policy-check routes exist on any line.",
	"plan_summaries.go": "reads a plan by id for the AI summary surface; web-only.",
}

func TestNothingTheProviderReachesPrefersTheTFESurface(t *testing.T) {
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatal(err)
	}

	var scanned, withLiteral int
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		src, err := os.ReadFile(filepath.Clean(name))
		if err != nil {
			t.Fatal(err)
		}
		scanned++
		hits := tfePath.FindAllString(string(src), -1)
		if len(hits) == 0 {
			continue
		}
		withLiteral++
		if reason := notProviderFacing[name]; reason != "" {
			continue
		}
		t.Errorf("%s reaches the TFE-compatibility surface (%d path literal(s), e.g. %s).\n"+
			"That surface is for terraform/tofu/tfci: it is engine-filtered and strips "+
			"native-only attributes from its responses, so a consumer that reads back what "+
			"it wrote sees fields it set as null (#2469). Use the native path. If this file "+
			"genuinely has no native route to use, add it to notProviderFacing with the "+
			"reason -- but only if the provider cannot reach it.", name, len(hits), hits[0])
	}

	// Floors, so a walk that stops matching fails rather than passing on nothing.
	if scanned < 40 {
		t.Fatalf("only %d non-test files scanned; the walk is not finding the package", scanned)
	}
	if withLiteral != len(notProviderFacing) {
		t.Fatalf("%d files carry a TFE literal but %d are exempt; if that is now fewer, "+
			"remove the discharged exemption rather than leaving the count adrift",
			withLiteral, len(notProviderFacing))
	}
}

// TestTheExemptionsAreStillAccurate keeps the list honest from the other
// direction: an entry naming a file that no longer calls that surface is a
// stale constraint, and the next reader takes it for a live one.
func TestTheExemptionsAreStillAccurate(t *testing.T) {
	for name, reason := range notProviderFacing {
		if reason == "" {
			t.Errorf("%s is exempt with no reason; an exemption that does not say what is "+
				"missing natively cannot be reviewed", name)
		}
		src, err := os.ReadFile(filepath.Clean(name))
		if err != nil {
			t.Errorf("%s is exempt but does not exist", name)
			continue
		}
		if !tfePath.Match(src) {
			t.Errorf("%s no longer calls the TFE surface -- remove its exemption rather "+
				"than leaving a constraint that has been discharged", name)
		}
	}
}

// TestTheFallbackMachineryIsGone pins the removal itself.
//
// The helpers were the only way a native consumer could end up on the
// compatibility surface without saying so at the call site, and the cheapest
// way to "fix" a native route that a server does not serve is to reintroduce
// one. If a skew window is ever actually promised for the provider or the SDK,
// that is a decision to take in `docs/versioning-and-support.md` first -- and
// then this test is the thing that has to be deleted deliberately.
func TestTheFallbackMachineryIsGone(t *testing.T) {
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatal(err)
	}
	banned := regexp.MustCompile(`nativeFirst|nativeFirstErr|routeAbsent|(?:get|post|patch|delete|deleteWithBody)Dual\(`)
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".go") {
			continue
		}
		// This file names them in order to ban them.
		if name == "tfe_surface_test.go" {
			continue
		}
		src, err := os.ReadFile(filepath.Clean(name))
		if err != nil {
			t.Fatal(err)
		}
		if m := banned.FindString(string(src)); m != "" {
			t.Errorf("%s reintroduces the TFE fallback (%q). The provider and this SDK "+
				"target the same major as the API; a fallback is how that quietly stops "+
				"being true.", name, m)
		}
	}
}
