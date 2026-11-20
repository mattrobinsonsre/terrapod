package autodiscovery_rule

import (
	"encoding/json"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/types"
	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// `propagate` is a per-request OPTION, not a field of the rule, so it belongs at
// the document's top level beside `data` and must never reach `attributes`
// (#2621 item 53). A rule field named `propagate` would be rejected by the API
// as an unknown attribute, and the propagation would silently not happen.
func TestPropagateIsNeverARuleAttribute(t *testing.T) {
	for _, val := range []types.Bool{types.BoolValue(true), types.BoolValue(false), types.BoolNull()} {
		attrs := buildAutodiscoveryRuleAttrs(&autodiscoveryRuleModel{Propagate: val})
		for _, key := range []string{"propagate", "dry-run", "dry_run"} {
			if _, ok := attrs[key]; ok {
				t.Fatalf("propagate=%v put %q in the rule's attributes", val, key)
			}
		}
	}
}

// The shape the API reads: `_bool_option(body, "propagate")` looks at the top
// level, so an option nested anywhere else is not seen at all.
func TestPropagateTravelsAtTheDocumentTopLevel(t *testing.T) {
	attrs := map[string]any{"name": "rule-1"}

	body, err := terrapod.MarshalResourceWithOptions(
		"adr-1", "autodiscovery-rules", attrs, nil, map[string]any{"propagate": true},
	)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var doc map[string]json.RawMessage
	if err := json.Unmarshal(body, &doc); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got := string(doc["propagate"]); got != "true" {
		t.Errorf("top-level propagate = %q, want true", got)
	}
	if _, ok := doc["data"]; !ok {
		t.Error("the document lost its data member")
	}

	// And the negative: with no options the key is absent rather than false, so
	// an ordinary update cannot be mistaken for a declined propagation.
	plain, err := terrapod.MarshalResourceWithOptions(
		"adr-1", "autodiscovery-rules", attrs, nil, nil,
	)
	if err != nil {
		t.Fatalf("marshal plain: %v", err)
	}
	var plainDoc map[string]json.RawMessage
	if err := json.Unmarshal(plain, &plainDoc); err != nil {
		t.Fatalf("unmarshal plain: %v", err)
	}
	if _, ok := plainDoc["propagate"]; ok {
		t.Error("an update with no options carries a propagate key")
	}
}

// `data` is the payload, never an option. Overwriting it would send an update
// that changes nothing and report success, so the helper refuses.
func TestOptionsCannotDisplaceTheDocument(t *testing.T) {
	_, err := terrapod.MarshalResourceWithOptions(
		"adr-1", "autodiscovery-rules", map[string]any{"name": "x"}, nil,
		map[string]any{"data": "clobbered"},
	)
	if err == nil {
		t.Fatal("an option named data was accepted; it would replace the payload")
	}
}
