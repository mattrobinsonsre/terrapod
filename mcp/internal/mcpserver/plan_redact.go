package mcpserver

import (
	"bytes"
	"encoding/json"
)

// Redacting the plan JSON this server hands to an agent (GHSA-3g53-5gw3-hh42).
//
// terrapod_run_plan_json returns the whole `tofu show -json` document, and it
// is annotated read-only. Read-only is true and beside the point: the hint
// tells an MCP host the call is safe to make without asking the operator, and
// what makes it unsafe is not that it writes but that it reads secrets out to
// a model.
//
// This is written here rather than cherry-picked: the line it came from splits
// the tool into compact and full views and redacts as part of building the
// compact one, which is a feature this line does not have. Only the redaction
// belongs in a security backport.

const sensitivePlaceholder = "(sensitive value)"

// secretSet holds values known to be secret anywhere in a plan.
type secretSet map[string]struct{}

// planSecrets collects every string the plan marks sensitive, in any
// resource's before or after.
//
// Markers alone are not enough to redact safely, which is why the values are
// collected and then matched anywhere rather than blanked in place. A value
// derived from a sensitive one does not always keep the marking: a
// terraform_data that copies a sensitive input to its output is marked on the
// input and not on the output, so the prior secret sits there in the clear.
// The cost is occasionally hiding a harmless value that happens to equal one,
// which is the right way to be wrong here.
func planSecrets(raw []byte) secretSet {
	var doc map[string]any
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&doc); err != nil {
		return nil
	}
	s := secretSet{}
	for _, key := range []string{"resource_changes", "resource_drift"} {
		entries, _ := doc[key].([]any)
		for _, e := range entries {
			entry, _ := e.(map[string]any)
			change, _ := entry["change"].(map[string]any)
			if change == nil {
				continue
			}
			for _, side := range []string{"before", "after"} {
				collectMarked(change[side], change[side+"_sensitive"], s)
			}
		}
	}
	outputs, _ := doc["output_changes"].(map[string]any)
	for _, o := range outputs {
		change, _ := o.(map[string]any)
		if change == nil {
			continue
		}
		for _, side := range []string{"before", "after"} {
			collectMarked(change[side], change[side+"_sensitive"], s)
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

// collectLeaves adds every string under v. Empty strings, numbers and booleans
// are skipped: as secrets they carry almost nothing, and matching them would
// redact half the plan.
func collectLeaves(v any, s secretSet) {
	switch x := v.(type) {
	case string:
		if x != "" {
			s[x] = struct{}{}
		}
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

// redactPlanJSON returns the document with every known secret replaced.
//
// Returns the input unchanged when it will not parse, or when nothing is
// marked: a document we cannot read is also one the agent can make nothing of,
// and re-serialising a plan with no secrets in it would only reformat it.
func redactPlanJSON(raw []byte) []byte {
	secrets := planSecrets(raw)
	if len(secrets) == 0 {
		return raw
	}
	var doc any
	dec := json.NewDecoder(bytes.NewReader(raw))
	// UseNumber so the document round-trips exactly: a plain decode turns
	// 1234567890123456789 into ...800, quietly changing ids and
	// epoch-nanosecond timestamps in a document served as "the plan".
	dec.UseNumber()
	if err := dec.Decode(&doc); err != nil {
		return raw
	}
	out, err := json.Marshal(redactDocument(doc, secrets))
	if err != nil {
		return raw
	}
	return out
}

// redactDocument replaces every known secret leaf anywhere in the document.
func redactDocument(v any, secrets secretSet) any {
	switch x := v.(type) {
	case string:
		if _, ok := secrets[x]; ok {
			return sensitivePlaceholder
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
