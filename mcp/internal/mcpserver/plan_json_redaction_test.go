package mcpserver

import (
	"bytes"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
)

// view=full used to return the raw document while being advertised to hosts as
// read-only (GHSA-3g53-5gw3-hh42). Read-only was true and beside the point: the
// hint tells a host the call is safe to make without asking the operator, and
// what made it unsafe was not that it wrote but that it read secrets out.

const planWithSecret = `{
  "format_version": "1.2",
  "terraform_version": "1.9.0",
  "resource_changes": [
    {
      "address": "aws_db_instance.main",
      "type": "aws_db_instance",
      "name": "main",
      "change": {
        "actions": ["update"],
        "before": {"password": "OLD-PLAN-SECRET-aaaa", "engine": "postgres"},
        "after": {"password": "NEW-PLAN-SECRET-bbbb", "engine": "postgres"},
        "before_sensitive": {"password": true},
        "after_sensitive": {"password": true}
      }
    },
    {
      "address": "terraform_data.copy",
      "type": "terraform_data",
      "name": "copy",
      "change": {
        "actions": ["create"],
        "after": {"input": "NEW-PLAN-SECRET-bbbb"},
        "after_sensitive": {}
      }
    }
  ]
}`

func TestPlanJSONFullViewRedactsSensitiveValues(t *testing.T) {
	sess, _ := planServer(t, http.StatusOK, []byte(planWithSecret))
	res, out := callPlanJSON(t, sess, map[string]any{"run_id": "run-aaaa", "view": "full"})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	body, _ := json.Marshal(out)
	for _, secret := range []string{"OLD-PLAN-SECRET-aaaa", "NEW-PLAN-SECRET-bbbb"} {
		if strings.Contains(string(body), secret) {
			t.Errorf("view=full leaked %s", secret)
		}
	}
	if !strings.Contains(string(body), sensitivePlaceholder) {
		t.Error("nothing was redacted; the fixture or the walk is wrong")
	}
	// Non-sensitive values must survive, or the view is useless.
	if !strings.Contains(string(body), "postgres") {
		t.Error("view=full dropped a non-sensitive value")
	}
}

// The reason redaction walks the whole document rather than the marked
// positions: terraform_data copies a sensitive input to an unmarked output, so
// the same secret sits there with no marker on it.
func TestPlanJSONFullViewRedactsAnUnmarkedCopy(t *testing.T) {
	sess, _ := planServer(t, http.StatusOK, []byte(planWithSecret))
	_, out := callPlanJSON(t, sess, map[string]any{"run_id": "run-aaaa", "view": "full"})
	doc, ok := out["plan_json"].(map[string]any)
	if !ok {
		t.Fatalf("no plan_json object in %v", out)
	}
	changes, _ := doc["resource_changes"].([]any)
	if len(changes) != 2 {
		t.Fatalf("expected 2 resource changes, got %d", len(changes))
	}
	copied, _ := changes[1].(map[string]any)
	change, _ := copied["change"].(map[string]any)
	after, _ := change["after"].(map[string]any)
	if got := after["input"]; got != sensitivePlaceholder {
		t.Errorf("the unmarked copy was not redacted: got %v", got)
	}
}

// Paging must be over the redacted bytes. Redacting each chunk as it was served
// would let a secret straddling a boundary survive, and would hand the agent
// offsets that did not match the bytes it received.
func TestPlanJSONFullViewPagesOverRedactedBytes(t *testing.T) {
	sess, _ := planServer(t, http.StatusOK, []byte(planWithSecret))
	var assembled strings.Builder
	var offset float64
	for range 20 {
		_, out := callPlanJSON(t, sess, map[string]any{
			"run_id": "run-aaaa", "view": "full", "max_bytes": 64, "offset": offset,
		})
		chunk, _ := out["plan_json_text"].(string)
		assembled.WriteString(chunk)
		next, ok := out["next_offset"].(float64)
		if !ok || next <= offset {
			break
		}
		offset = next
	}
	whole := assembled.String()
	if whole == "" {
		t.Fatal("paging returned nothing")
	}
	for _, secret := range []string{"OLD-PLAN-SECRET-aaaa", "NEW-PLAN-SECRET-bbbb"} {
		if strings.Contains(whole, secret) {
			t.Errorf("a paged chunk leaked %s", secret)
		}
	}
}

// Redaction re-serialises the document, and a plain JSON round-trip through
// `any` decodes every number as float64 — turning 1234567890123456789 into
// 1234567890123456800. A resource id or an epoch-nanosecond timestamp quietly
// changing value in the document we hand back as "the plan" would be its own
// bug, so the redaction decodes with UseNumber and returns numbers untouched.
func TestRedactDocumentPreservesNumberPrecision(t *testing.T) {
	raw := []byte(`{"serial": 1234567890123456789, "p": "SEKRIT-VALUE-zzzz"}`)
	var doc any
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&doc); err != nil {
		t.Fatal(err)
	}
	out, err := json.Marshal(redactDocument(doc, secretSet{"SEKRIT-VALUE-zzzz": struct{}{}}))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(out), "1234567890123456789") {
		t.Errorf("redaction lost number precision: %s", out)
	}
	if strings.Contains(string(out), "SEKRIT-VALUE-zzzz") {
		t.Errorf("redaction did not redact: %s", out)
	}
}

// And end to end on the paging path, which returns the document as text and so
// is the one that actually carries the digits to the agent.
//
// The parsed `plan_json` path does NOT preserve them: the MCP SDK re-marshals
// structured content through `any`, so a large number arrives rounded. That is
// pre-existing — before redaction that path decoded with a plain Unmarshal and
// was already lossy — and it is not what this advisory is about. Noted here so
// the next reader does not mistake it for something redaction broke.
func TestPlanJSONFullViewPagingPreservesLargeNumbers(t *testing.T) {
	const plan = `{"format_version":"1.2","resource_changes":[{"address":"aws_db_instance.main","change":{"actions":["update"],"after":{"password":"PLAN-SECRET-cccc","serial":1234567890123456789},"after_sensitive":{"password":true}}}]}`
	sess, _ := planServer(t, http.StatusOK, []byte(plan))
	var assembled strings.Builder
	var offset float64
	for range 20 {
		res, out := callPlanJSON(t, sess, map[string]any{
			"run_id": "run-aaaa", "view": "full", "max_bytes": 48, "offset": offset,
		})
		if res.IsError {
			t.Fatalf("tool error: %s", resultText(t, res))
		}
		chunk, _ := out["plan_json_text"].(string)
		assembled.WriteString(chunk)
		next, ok := out["next_offset"].(float64)
		if !ok || next <= offset {
			break
		}
		offset = next
	}
	whole := assembled.String()
	if strings.Contains(whole, "PLAN-SECRET-cccc") {
		t.Error("the secret survived paging")
	}
	if !strings.Contains(whole, "1234567890123456789") {
		t.Errorf("paging lost number precision: %s", whole)
	}
}
