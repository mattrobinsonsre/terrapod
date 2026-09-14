package mcpserver

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

// testdata/plan_small.json is a real `tofu show -json` document (OpenTofu
// 1.12), made with the terraform_data resource so it needs no provider or
// credentials. Against a prior apply it plans every kind of change:
//
//	terraform_data.added[0], added[1]  create
//	terraform_data.credential          update  (a sensitive input)
//	terraform_data.kept                no-op
//	terraform_data.removed             delete
//	terraform_data.replaced            delete+create (triggers_replace)
//	terraform_data.updated             update  ("hello" -> "hello, world")
//
// The sensitive variable's values are s3cr3t-one (before) and s3cr3t-two
// (after); neither may appear in the compact view. Note that tofu marks the
// credential's input sensitive but not its output, which holds the same
// secret — the fixture is real, so that gap is real too.

func loadPlanFixture(t *testing.T) []byte {
	t.Helper()
	raw, err := os.ReadFile("testdata/plan_small.json")
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func fixturePlan(t *testing.T) *tfPlan {
	t.Helper()
	p, err := parsePlan(loadPlanFixture(t))
	if err != nil {
		t.Fatalf("parse fixture: %v", err)
	}
	return p
}

func byAddress(changes []planChange) map[string]planChange {
	m := map[string]planChange{}
	for _, c := range changes {
		m[c.Address] = c
	}
	return m
}

func TestSummaryMatchesTofusPlanLine(t *testing.T) {
	// tofu reports this plan as "3 to add, 2 to change, 2 to destroy": the
	// replacement counts on both sides.
	got := summarise(fixturePlan(t))
	want := planSummary{Add: 3, Change: 2, Destroy: 2, Replace: 1}
	if got != want {
		t.Errorf("summary = %+v, want %+v", got, want)
	}
}

func TestCompactViewLeavesOutNoOps(t *testing.T) {
	got := byAddress(compactChanges(fixturePlan(t), planFilter{}))
	if len(got) != 6 {
		t.Errorf("got %d changes, want 6: %v", len(got), got)
	}
	if _, ok := got["terraform_data.kept"]; ok {
		t.Error("a no-op resource is in the default view")
	}
}

func TestCompactViewShowsOnlyWhatChanges(t *testing.T) {
	got := byAddress(compactChanges(fixturePlan(t), planFilter{}))

	upd := got["terraform_data.updated"]
	in, ok := upd.Changed["input"]
	if !ok {
		t.Fatalf("updated: input is not listed as changing: %+v", upd.Changed)
	}
	if in.Before != "hello" || in.After != "hello, world" {
		t.Errorf("updated.input = %v -> %v, want hello -> hello, world", in.Before, in.After)
	}
	if _, ok := upd.Changed["id"]; ok {
		t.Error("updated: an unchanged attribute (id) is listed as changing")
	}
}

func TestCompactViewRedactsSensitiveValues(t *testing.T) {
	changes := compactChanges(fixturePlan(t), planFilter{})
	b, _ := json.Marshal(changes)
	if i := strings.Index(string(b), "s3cr3t"); i >= 0 {
		t.Fatalf("a sensitive value leaked into the compact view: …%s…", b[max(i-120, 0):min(i+40, len(b))])
	}
	cred := byAddress(changes)["terraform_data.credential"].Changed
	if in := cred["input"]; in.Before != sensitivePlaceholder || in.After != sensitivePlaceholder {
		t.Errorf("credential.input = %v -> %v, want both redacted", in.Before, in.After)
	}
	// The unmarked copy of the secret is redacted too.
	if out := cred["output"]; out.Before != sensitivePlaceholder {
		t.Errorf("credential.output before = %v, want it redacted", out.Before)
	}
}

func TestCompactViewForACreateShowsTheNewValuesAndWhatIsNotKnownYet(t *testing.T) {
	c := byAddress(compactChanges(fixturePlan(t), planFilter{}))["terraform_data.added[0]"]
	if got := c.Changed["input"]; got.Before != nil || got.After != "new 0" {
		t.Errorf("added[0].input = %v -> %v, want nil -> new 0", got.Before, got.After)
	}
	// id is not known until apply, so tofu leaves it out of `after`
	// entirely; it has to be found through after_unknown.
	for _, k := range []string{"id", "output"} {
		if got := c.Changed[k].After; got != unknownPlaceholder {
			t.Errorf("added[0].%s after = %v, want %q", k, got, unknownPlaceholder)
		}
	}
}

func TestCompactViewForADeleteListsNoAttributes(t *testing.T) {
	c := byAddress(compactChanges(fixturePlan(t), planFilter{}))["terraform_data.removed"]
	if len(c.Changed) != 0 {
		t.Errorf("a deletion lists attributes: %+v", c.Changed)
	}
}

func TestCompactViewForAReplaceSaysWhyAndWhatChanged(t *testing.T) {
	c := byAddress(compactChanges(fixturePlan(t), planFilter{}))["terraform_data.replaced"]
	if !isReplace(c.Actions) {
		t.Errorf("replaced: actions = %v", c.Actions)
	}
	if c.ReplacePaths == nil {
		t.Error("replaced: replace_paths is missing, so the reason for the replacement is lost")
	}
	if _, ok := c.Changed["triggers_replace"]; !ok {
		t.Errorf("replaced: triggers_replace is not listed as changing: %+v", c.Changed)
	}
}

func TestFilters(t *testing.T) {
	p := fixturePlan(t)
	cases := []struct {
		name   string
		filter planFilter
		want   []string
	}{
		{"address prefix", planFilter{Address: "terraform_data.added"},
			[]string{"terraform_data.added[0]", "terraform_data.added[1]"}},
		{"address glob", planFilter{Address: "terraform_data.*ed"},
			[]string{"terraform_data.removed", "terraform_data.replaced", "terraform_data.updated"}},
		{"replace", planFilter{Actions: []string{"replace"}}, []string{"terraform_data.replaced"}},
		// A replacement destroys too, so asking what is destroyed includes it.
		{"delete", planFilter{Actions: []string{"delete"}},
			[]string{"terraform_data.removed", "terraform_data.replaced"}},
		{"no-op on request", planFilter{Actions: []string{"no-op"}}, []string{"terraform_data.kept"}},
		// Brackets are an index, not a character class.
		{"address with an index", planFilter{Address: "terraform_data.added[1]", Actions: []string{"create"}},
			[]string{"terraform_data.added[1]"}},
		{"glob over indexes", planFilter{Address: "terraform_data.added[*]"},
			[]string{"terraform_data.added[0]", "terraform_data.added[1]"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var got []string
			for _, c := range compactChanges(p, tc.filter) {
				got = append(got, c.Address)
			}
			if strings.Join(got, ",") != strings.Join(tc.want, ",") {
				t.Errorf("got %v, want %v", got, tc.want)
			}
		})
	}
}

func TestFilterValidation(t *testing.T) {
	if err := (planFilter{Actions: []string{"destroy"}}).validate(); err == nil ||
		!strings.Contains(err.Error(), "delete") {
		t.Errorf("an unknown action should be refused with the valid ones listed, got %v", err)
	}
	if err := (planFilter{Address: `aws_*\`}).validate(); err == nil {
		t.Error("a malformed glob should be refused")
	}
	if err := (planFilter{Address: "aws_instance.[x"}).validate(); err != nil {
		t.Errorf("a bracket is literal, so this is a valid prefix: %v", err)
	}
	if err := (planFilter{Address: "module.a", Actions: []string{"replace", "no-op"}}).validate(); err != nil {
		t.Errorf("a valid filter was refused: %v", err)
	}
}

func TestALargeValueIsReplacedBySizeNote(t *testing.T) {
	big := strings.Repeat("x", maxAttrBytes+1)
	rc := tfResourceChange{Address: "aws_iam_policy.p"}
	rc.Change.Actions = []string{"update"}
	rc.Change.Before = map[string]any{"policy": "small"}
	rc.Change.After = map[string]any{"policy": big}

	got, _ := compact(rc, nil).Changed["policy"].After.(string)
	if !strings.Contains(got, "too large") || strings.Contains(got, big) {
		t.Errorf("a large value was not capped: %.80q", got)
	}
}

func TestANestedSensitiveValueRedactsTheWholeAttribute(t *testing.T) {
	rc := tfResourceChange{Address: "x.y"}
	rc.Change.Actions = []string{"update"}
	rc.Change.Before = map[string]any{"settings": map[string]any{"user": "a", "password": "p1"}}
	rc.Change.After = map[string]any{"settings": map[string]any{"user": "a", "password": "p2"}}
	rc.Change.BeforeSensitive = map[string]any{"settings": map[string]any{"password": true}}
	rc.Change.AfterSensitive = map[string]any{"settings": map[string]any{"password": true}}

	got := compact(rc, nil).Changed["settings"]
	if got.Before != sensitivePlaceholder || got.After != sensitivePlaceholder {
		t.Errorf("settings = %v -> %v, want the whole attribute redacted", got.Before, got.After)
	}
}

func TestASensitiveVariablesValueIsRedactedWhereverItTurnsUp(t *testing.T) {
	// A root variable declared sensitive, whose value reaches an attribute
	// that carries no sensitivity marker at all.
	var p tfPlan
	if err := json.Unmarshal([]byte(`{
	  "variables": {"db_password": {"value": "hunter2-long"}, "region": {"value": "eu-west-1"}},
	  "configuration": {"root_module": {"variables": {"db_password": {"sensitive": true}, "region": {}}}},
	  "resource_changes": [{
	    "address": "aws_ssm_parameter.p",
	    "change": {"actions": ["update"],
	      "before": {"value": "old", "region": "eu-west-1"},
	      "after": {"value": "hunter2-long", "region": "eu-west-2"}}
	  }]
	}`), &p); err != nil {
		t.Fatal(err)
	}
	got := compactChanges(&p, planFilter{})[0].Changed
	if got["value"].After != sensitivePlaceholder {
		t.Errorf("value after = %v, want it redacted", got["value"].After)
	}
	// A non-sensitive variable's value is shown as normal.
	if got["region"].Before != "eu-west-1" {
		t.Errorf("region before = %v, want it shown", got["region"].Before)
	}
}
