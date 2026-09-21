package mcpserver

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// terrapod_run_plan_json returns the whole plan document and is annotated
// read-only, so a host may call it without asking the operator
// (GHSA-3g53-5gw3-hh42). It must not be a way to read secrets out.

const planWithSecret = `{
  "format_version": "1.2",
  "resource_changes": [
    {
      "address": "aws_db_instance.main",
      "change": {
        "actions": ["update"],
        "before": {"password": "OLD-PLAN-SECRET-aaaa", "engine": "postgres"},
        "after": {"password": "NEW-PLAN-SECRET-bbbb", "engine": "postgres", "serial": 1234567890123456789},
        "before_sensitive": {"password": true},
        "after_sensitive": {"password": true}
      }
    },
    {
      "address": "terraform_data.copy",
      "change": {
        "actions": ["create"],
        "after": {"input": "NEW-PLAN-SECRET-bbbb"},
        "after_sensitive": {}
      }
    }
  ],
  "output_changes": {
    "url": {"actions": ["create"], "after": "postgres://u:OUTPUT-SECRET-cccc@h/d", "after_sensitive": true}
  }
}`

func TestRedactPlanJSONRemovesMarkedValues(t *testing.T) {
	out := string(redactPlanJSON([]byte(planWithSecret)))
	for _, secret := range []string{"OLD-PLAN-SECRET-aaaa", "NEW-PLAN-SECRET-bbbb", "OUTPUT-SECRET-cccc"} {
		if strings.Contains(out, secret) {
			t.Errorf("leaked %s", secret)
		}
	}
	if !strings.Contains(out, sensitivePlaceholder) {
		t.Error("nothing was redacted")
	}
	if !strings.Contains(out, "postgres") {
		t.Error("a non-sensitive value was dropped")
	}
}

// The reason values are collected and matched anywhere rather than blanked in
// place: terraform_data copies a sensitive input to an unmarked output.
func TestRedactPlanJSONRemovesAnUnmarkedCopy(t *testing.T) {
	var doc map[string]any
	if err := json.Unmarshal(redactPlanJSON([]byte(planWithSecret)), &doc); err != nil {
		t.Fatal(err)
	}
	changes, _ := doc["resource_changes"].([]any)
	if len(changes) != 2 {
		t.Fatalf("expected 2 changes, got %d", len(changes))
	}
	copied, _ := changes[1].(map[string]any)
	change, _ := copied["change"].(map[string]any)
	after, _ := change["after"].(map[string]any)
	if got := after["input"]; got != sensitivePlaceholder {
		t.Errorf("the unmarked copy was not redacted: got %v", got)
	}
}

// A plain decode through `any` turns 1234567890123456789 into ...800, so an id
// or an epoch-nanosecond timestamp would quietly change value in a document
// served as "the plan".
func TestRedactPlanJSONPreservesLargeNumbers(t *testing.T) {
	out := string(redactPlanJSON([]byte(planWithSecret)))
	if !strings.Contains(out, "1234567890123456789") {
		t.Errorf("lost number precision: %s", out)
	}
}

func TestRedactPlanJSONNeverFailsTheCall(t *testing.T) {
	for _, raw := range []string{"not json", "[1,2,3]", ""} {
		if got := string(redactPlanJSON([]byte(raw))); got != raw {
			t.Errorf("redactPlanJSON(%q) = %q, want it returned unchanged", raw, got)
		}
	}
}

func TestRedactPlanJSONLeavesAPlanWithNoSecretsByteIdentical(t *testing.T) {
	// Nothing marked means nothing to do; re-serialising would only reformat
	// a document the agent is about to read.
	raw := `{"resource_changes":[{"change":{"actions":["create"],"after":{"bucket":"logs"}}}]}`
	if got := string(redactPlanJSON([]byte(raw))); got != raw {
		t.Errorf("reformatted a plan with no secrets:\n got %s\nwant %s", got, raw)
	}
}

// And through the tool itself. Testing redactPlanJSON alone passes happily
// while the handler ignores it, so the call path is asserted here.
//
// On this line the call does not succeed: plan_json is a json.RawMessage,
// which the SDK types as "null or array", so every real plan (an object) fails
// output validation. That is a pre-existing functional bug, fixed upstream by
// the compact/full view rework, and out of scope for a security patch.
//
// It is not out of scope for THIS test, because the validation error quotes
// the whole document back — so before redaction the tool leaked the plan
// through its own error message. What is asserted is the security property
// that holds either way: whatever the tool hands back, secrets are not in it.
func TestPlanJSONToolRedactsThroughTheHandler(t *testing.T) {
	sess := toolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasSuffix(r.URL.Path, "/json-output") {
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(planWithSecret))
	})
	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "terrapod_run_plan_json", Arguments: map[string]any{"run_id": "run-aaaa"},
	})

	seen := ""
	if err != nil {
		seen = err.Error()
	} else {
		seen = resultText(t, res)
	}
	if seen == "" {
		t.Fatal("the tool returned neither an error nor a result")
	}
	for _, secret := range []string{"OLD-PLAN-SECRET-aaaa", "NEW-PLAN-SECRET-bbbb", "OUTPUT-SECRET-cccc"} {
		if strings.Contains(seen, secret) {
			t.Errorf("the tool leaked %s", secret)
		}
	}
	if !strings.Contains(seen, sensitivePlaceholder) {
		t.Error("nothing reaching the agent was redacted; is the handler still calling redactPlanJSON?")
	}
}
