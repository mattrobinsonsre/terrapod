package mcpserver

import (
	"encoding/json"
	"fmt"
	"path"
	"reflect"
	"slices"
	"strings"
)

// The compact view of a `tofu show -json` plan that terrapod_run_plan_json
// returns by default (#1601). A real plan document is routinely megabytes —
// prior state, configuration, every attribute of every resource — which is far
// more than an agent can use. What an agent wants from a plan is what it will
// DO, so this keeps the changing resources and, for each, only the attributes
// that change.

// Placeholders that stand in for values the compact view will not show.
const (
	sensitivePlaceholder = "(sensitive value)"
	unknownPlaceholder   = "(known after apply)"
)

// maxAttrBytes caps one attribute's rendered value in the compact view. A
// single value — an IAM policy document, a rendered template — can be larger
// than the rest of the plan together; past this size it is replaced by a note
// saying how big it is and where to read it.
const maxAttrBytes = 2048

// planActionFilters are the values the `actions` filter accepts. "replace" is
// not a tofu action — a replacement is delete+create — but it is how people
// ask for one, so it matches either order of that pair.
var planActionFilters = []string{"create", "update", "delete", "replace", "read", "no-op"}

// tfPlan is the subset of a `tofu show -json` document the compact view reads.
type tfPlan struct {
	FormatVersion    string             `json:"format_version"`
	TerraformVersion string             `json:"terraform_version"`
	Errored          bool               `json:"errored"`
	ResourceChanges  []tfResourceChange `json:"resource_changes"`
	Variables        map[string]struct {
		Value any `json:"value"`
	} `json:"variables"`
	Configuration struct {
		RootModule struct {
			Variables map[string]struct {
				Sensitive bool `json:"sensitive"`
			} `json:"variables"`
		} `json:"root_module"`
	} `json:"configuration"`
}

type tfResourceChange struct {
	Address      string `json:"address"`
	ActionReason string `json:"action_reason"`
	Change       struct {
		Actions         []string `json:"actions"`
		Before          any      `json:"before"`
		After           any      `json:"after"`
		AfterUnknown    any      `json:"after_unknown"`
		BeforeSensitive any      `json:"before_sensitive"`
		AfterSensitive  any      `json:"after_sensitive"`
		ReplacePaths    any      `json:"replace_paths"`
		Importing       any      `json:"importing"`
	} `json:"change"`
}

// planSummary counts the plan's changes the way tofu's own
// "Plan: N to add, N to change, N to destroy" line does, so the numbers match
// what a person reading the log sees: a replacement counts in both add and
// destroy, and separately in replace.
type planSummary struct {
	Add     int `json:"add"`
	Change  int `json:"change"`
	Destroy int `json:"destroy"`
	Replace int `json:"replace"`
	Import  int `json:"import"`
}

// planChangedAttr is one attribute's value either side of the change.
type planChangedAttr struct {
	Before any `json:"before"`
	After  any `json:"after"`
}

// planChange is one resource the plan acts on, reduced to what an agent
// needs to reason about it.
type planChange struct {
	Address      string                     `json:"address"`
	Actions      []string                   `json:"actions"`
	ActionReason string                     `json:"action_reason,omitempty"`
	ReplacePaths any                        `json:"replace_paths,omitempty"`
	Importing    bool                       `json:"importing,omitempty"`
	Changed      map[string]planChangedAttr `json:"changed,omitempty"`
}

// planFilter narrows the compact view.
type planFilter struct {
	// Address is a prefix, or a glob when it contains * or ?. Square brackets
	// are always literal: they are how an address writes an index
	// (aws_instance.web[0], module.m["k"]), so "added[1]" means that instance,
	// not a character class.
	Address string
	// Actions keeps a change when any of its actions is listed ("replace"
	// matches a delete+create pair). Empty means every action except no-op.
	Actions []string
}

// validate reports a filter the compact view cannot apply, in words an agent
// can act on.
func (f planFilter) validate() error {
	for _, a := range f.Actions {
		if !slices.Contains(planActionFilters, a) {
			return fmt.Errorf("unknown action %q in actions; use any of %s", a, strings.Join(planActionFilters, ", "))
		}
	}
	if isGlob(f.Address) {
		if _, err := path.Match(globPattern(f.Address), ""); err != nil {
			return fmt.Errorf("address %q is not a valid glob: %v", f.Address, err)
		}
	}
	return nil
}

func isGlob(s string) bool { return strings.ContainsAny(s, "*?") }

// globPattern escapes an address glob's brackets so path.Match reads them
// literally.
func globPattern(s string) string {
	return strings.NewReplacer("[", `\[`, "]", `\]`).Replace(s)
}

func (f planFilter) keeps(rc tfResourceChange) bool {
	if f.Address != "" {
		if isGlob(f.Address) {
			if ok, _ := path.Match(globPattern(f.Address), rc.Address); !ok {
				return false
			}
		} else if !strings.HasPrefix(rc.Address, f.Address) {
			return false
		}
	}
	actions := rc.Change.Actions
	if len(f.Actions) == 0 {
		return !isNoOp(actions)
	}
	for _, want := range f.Actions {
		if want == "replace" && isReplace(actions) {
			return true
		}
		if slices.Contains(actions, want) {
			return true
		}
	}
	return false
}

func isNoOp(actions []string) bool {
	return len(actions) == 0 || (len(actions) == 1 && actions[0] == "no-op")
}

func isReplace(actions []string) bool {
	return len(actions) == 2 && slices.Contains(actions, "delete") && slices.Contains(actions, "create")
}

// parsePlan decodes a `tofu show -json` document.
func parsePlan(raw []byte) (*tfPlan, error) {
	var p tfPlan
	if err := json.Unmarshal(raw, &p); err != nil {
		return nil, err
	}
	return &p, nil
}

// summarise counts every change in the plan, regardless of any filter.
func summarise(p *tfPlan) planSummary {
	var s planSummary
	for _, rc := range p.ResourceChanges {
		a := rc.Change.Actions
		switch {
		case isReplace(a):
			s.Add++
			s.Destroy++
			s.Replace++
		case slices.Contains(a, "create"):
			s.Add++
		case slices.Contains(a, "update"):
			s.Change++
		case slices.Contains(a, "delete"):
			s.Destroy++
		}
		if rc.Change.Importing != nil {
			s.Import++
		}
	}
	return s
}

// secretSet holds values known to be secret anywhere in a plan: strings and
// numbers, keyed by their decoded JSON value.
type secretSet map[any]struct{}

// planSecrets collects every value the plan marks sensitive — in any
// resource's before or after — plus the values of root variables declared
// sensitive.
//
// tofu's markers alone are not enough to redact safely. A value derived from a
// sensitive one is not always marked: terraform_data copies a sensitive input
// to its output, and a real plan marks the input in before_sensitive but not
// the output, so the prior secret sits in the output in the clear. Redacting
// any attribute that holds a known secret closes that, at the cost of
// occasionally hiding a harmless value that happens to equal one — the right
// way to be wrong here.
func planSecrets(p *tfPlan) secretSet {
	s := secretSet{}
	for _, rc := range p.ResourceChanges {
		collectMarked(rc.Change.Before, rc.Change.BeforeSensitive, s)
		collectMarked(rc.Change.After, rc.Change.AfterSensitive, s)
	}
	for name, v := range p.Configuration.RootModule.Variables {
		if v.Sensitive {
			collectLeaves(p.Variables[name].Value, s)
		}
	}
	return s
}

// collectMarked walks a value alongside its sensitivity marker (true, or an
// object/array shadowing the value's shape) and collects the marked leaves.
func collectMarked(value, marker any, s secretSet) {
	switch m := marker.(type) {
	case bool:
		if m {
			collectLeaves(value, s)
		}
	case map[string]any:
		v, _ := value.(map[string]any)
		for k, sub := range m {
			collectMarked(v[k], sub, s)
		}
	case []any:
		v, _ := value.([]any)
		for i, sub := range m {
			if i < len(v) {
				collectMarked(v[i], sub, s)
			}
		}
	}
}

// collectLeaves adds every string and number under v. Empty strings and
// booleans are skipped: as secrets they are meaningless, and matching them
// would redact half the plan.
func collectLeaves(v any, s secretSet) {
	switch x := v.(type) {
	case string:
		if x != "" {
			s[x] = struct{}{}
		}
	case float64:
		s[x] = struct{}{}
	case map[string]any:
		for _, e := range x {
			collectLeaves(e, s)
		}
	case []any:
		for _, e := range x {
			collectLeaves(e, s)
		}
	}
}

// holdsSecret reports whether any leaf under v is a known secret.
func (s secretSet) holdsSecret(v any) bool {
	switch x := v.(type) {
	case string, float64:
		_, ok := s[x]
		return ok
	case map[string]any:
		for _, e := range x {
			if s.holdsSecret(e) {
				return true
			}
		}
	case []any:
		for _, e := range x {
			if s.holdsSecret(e) {
				return true
			}
		}
	}
	return false
}

// compactChanges returns the changes that pass the filter, in plan order.
func compactChanges(p *tfPlan, f planFilter) []planChange {
	secrets := planSecrets(p)
	var out []planChange
	for _, rc := range p.ResourceChanges {
		if !f.keeps(rc) {
			continue
		}
		out = append(out, compact(rc, secrets))
	}
	return out
}

func compact(rc tfResourceChange, secrets secretSet) planChange {
	c := rc.Change
	pc := planChange{
		Address:      rc.Address,
		Actions:      c.Actions,
		ActionReason: rc.ActionReason,
		ReplacePaths: c.ReplacePaths,
		Importing:    c.Importing != nil,
	}
	// A deletion changes nothing worth listing — the whole resource goes —
	// and a no-op or read has nothing changing by definition.
	if slices.Contains(c.Actions, "delete") && !isReplace(c.Actions) {
		return pc
	}
	if isNoOp(c.Actions) || slices.Equal(c.Actions, []string{"read"}) {
		return pc
	}

	before, _ := c.Before.(map[string]any)
	after, _ := c.After.(map[string]any)
	// A value not known until apply is left out of `after` altogether, so
	// after_unknown is the only place its key appears.
	unknowns, _ := c.AfterUnknown.(map[string]any)
	creating := slices.Equal(c.Actions, []string{"create"})

	changed := map[string]planChangedAttr{}
	for _, k := range unionKeys(before, after, unknowns) {
		unknown := marked(c.AfterUnknown, k)
		if creating {
			// Everything a new resource will have is news; skip only the
			// attributes that will be null and are not computed.
			if after[k] == nil && !unknown {
				continue
			}
		} else if !unknown && reflect.DeepEqual(before[k], after[k]) {
			continue
		}
		attr := planChangedAttr{
			Before: render(before[k], marked(c.BeforeSensitive, k), secrets),
			After:  render(after[k], marked(c.AfterSensitive, k), secrets),
		}
		if creating {
			attr.Before = nil
		}
		if unknown && after[k] == nil {
			attr.After = unknownPlaceholder
		}
		changed[k] = attr
	}
	if len(changed) > 0 {
		pc.Changed = changed
	}
	return pc
}

// marked reports whether a sensitivity or unknown marker covers key k. The
// marker is `true` for a whole object, or an object shadowing the value's
// shape; any marking at or below k counts, because redacting a whole attribute
// that holds one secret is safe and showing a partly-secret one is not.
func marked(marker any, k string) bool {
	switch m := marker.(type) {
	case bool:
		return m
	case map[string]any:
		return anyMarked(m[k])
	}
	return false
}

func anyMarked(v any) bool {
	switch m := v.(type) {
	case bool:
		return m
	case map[string]any:
		for _, e := range m {
			if anyMarked(e) {
				return true
			}
		}
	case []any:
		for _, e := range m {
			if anyMarked(e) {
				return true
			}
		}
	}
	return false
}

// render applies redaction and the size cap to one attribute value.
func render(v any, sensitive bool, secrets secretSet) any {
	if sensitive || secrets.holdsSecret(v) {
		return sensitivePlaceholder
	}
	if v == nil {
		return nil
	}
	b, err := json.Marshal(v)
	if err == nil && len(b) > maxAttrBytes {
		return fmt.Sprintf("(%d bytes, too large to show here — read it with view=full)", len(b))
	}
	return v
}

// unionKeys returns the keys of every map, sorted, without repeats.
func unionKeys(maps ...map[string]any) []string {
	var keys []string
	for _, m := range maps {
		for k := range m {
			if !slices.Contains(keys, k) {
				keys = append(keys, k)
			}
		}
	}
	slices.Sort(keys)
	return keys
}

// redactDocument replaces every known secret leaf anywhere in a decoded plan
// document, so `view=full` can return the whole thing without handing over the
// values (GHSA-3g53-5gw3-hh42).
//
// view=changes has always redacted; view=full did not, while both were
// advertised to hosts as read-only. Read-only is true and beside the point: the
// hint tells a host the call is safe to make without asking, and what made it
// unsafe was not that it wrote anything but that it read secrets out to a model.
//
// It walks the whole document rather than the marked positions because that is
// what `secrets` is for — a value copied from a sensitive attribute into an
// unmarked one keeps no marker, so position-based redaction would miss it. See
// planSecrets.
func redactDocument(v any, secrets secretSet) any {
	switch x := v.(type) {
	case string, float64:
		if _, ok := secrets[x]; ok {
			return sensitivePlaceholder
		}
		return v
	case json.Number:
		// Numbers are decoded with UseNumber so the document round-trips
		// exactly: a plain decode turns 1234567890123456789 into
		// ...800, silently corrupting ids and epoch-nanosecond timestamps in
		// a document we hand back as the plan. The secret set keys numbers as
		// float64, so match on that and return the original either way.
		if f, err := x.Float64(); err == nil {
			if _, ok := secrets[f]; ok {
				return sensitivePlaceholder
			}
		}
		return v
	case map[string]any:
		out := make(map[string]any, len(x))
		for k, e := range x {
			out[k] = redactDocument(e, secrets)
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = redactDocument(e, secrets)
		}
		return out
	}
	return v
}
