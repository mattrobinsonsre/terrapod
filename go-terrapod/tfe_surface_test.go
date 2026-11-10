package terrapod

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// Nothing the Terraform provider reaches may PREFER the TFE-compatibility
// surface (#2469).
//
// That surface exists for `terraform`, `tofu` and `tfci`. It is engine-filtered
// to Terraform and its serialiser strips every native-only attribute from its
// responses, so a consumer reading a resource there cannot see the fields it
// just wrote -- which is how `terrapod_workspace` came to fail every apply that
// set a configure attribute while the server stored the values correctly.
//
// This is NOT a ban on the literal, and the distinction is the whole point. A
// supported 1.x server serves `/api/terrapod/v1` and `/api/v2` and has no
// `/api/v1` mount at all, so for several resources the TFE route is the only
// way to reach that server and it survives as the LEGACY ARM of a native-first
// pair. What this refuses is a TFE path reached on its own, which is what the
// bug looked like at the call site.
//
// The rule is therefore positional: a function containing a TFE literal must
// also reach a native path, through `nativeFirst` or one of the `*Dual`
// helpers. The provider has the same gate over its own tree, and the web UI
// over its own (`web/tests/engine-scoped-prefixes.test.ts`).
var (
	tfePath    = regexp.MustCompile(`"/api/(?:tfe/)?v2[^"]*"`)
	nativePath = regexp.MustCompile(`"/api/(?:terrapod/)?v1[^"]*"`)
	pairing    = regexp.MustCompile(`nativeFirst|nativeFirstErr|(?:get|post|patch|delete|deleteWithBody)Dual\(`)
)

// notProviderFacing are the files whose TFE calls are NOT reached by the
// Terraform provider, with what each is waiting on. Measured, not assumed: the
// provider has zero call sites into any of them.
//
// They are exempt rather than fixed because moving them has a cost and no
// caller asking for it yet -- and because an exemption that states what is
// missing is reviewable, where silence is not. A file dropping off this list
// must earn it by pairing its calls, not by being forgotten.
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

	var checked, paired int
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		src, err := os.ReadFile(filepath.Clean(name))
		if err != nil {
			t.Fatal(err)
		}
		if !tfePath.Match(src) {
			continue
		}
		checked++
		if reason, exempt := notProviderFacing[name]; exempt {
			if reason == "" {
				t.Errorf("%s is exempt with no reason given; an exemption that does not "+
					"say what is missing natively cannot be reviewed", name)
			}
			continue
		}
		for _, fn := range topLevelFuncs(string(src)) {
			if !tfePath.MatchString(fn.body) {
				continue
			}
			paired++
			if !nativePath.MatchString(fn.body) || !pairing.MatchString(fn.body) {
				t.Errorf("%s: %s reaches the TFE surface without pairing it to a native "+
					"path.\nThe TFE serialiser strips native-only attributes from its "+
					"responses, so a caller that reads back what it wrote sees fields it set "+
					"as null (#2469). Use nativeFirst or a *Dual helper, keeping the TFE path "+
					"as the fallback arm.", name, fn.name)
			}
		}
	}

	// Floors, so a walk that stops matching fails rather than passing on nothing.
	if checked < len(notProviderFacing)+4 {
		t.Fatalf("only %d files with a TFE literal found; expected the %d exempt ones plus "+
			"the four provider-facing families", checked, len(notProviderFacing))
	}
	if paired < 15 {
		t.Fatalf("only %d paired call sites inspected; the walk is not finding them", paired)
	}
}

// TestTheExemptionsAreStillAccurate keeps the list above honest from the other
// direction: an entry naming a file that no longer has a TFE literal is a stale
// exemption, and the next person reads it as a live constraint.
func TestTheExemptionsAreStillAccurate(t *testing.T) {
	for name := range notProviderFacing {
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

type topFunc struct {
	name string
	body string
}

// topLevelFuncs cuts a file at top-level `func` declarations, which is all this
// needs: it has to attribute a literal to the function containing it, and a
// brace matcher would be a Go parser nobody wants here.
func topLevelFuncs(src string) []topFunc {
	decl := regexp.MustCompile(`^func (?:\([^)]*\) )?(\w+)`)
	var out []topFunc
	name, body := "", []string{}
	flush := func() {
		if name != "" {
			out = append(out, topFunc{name: name, body: strings.Join(body, "\n")})
		}
	}
	for _, l := range strings.Split(src, "\n") {
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
