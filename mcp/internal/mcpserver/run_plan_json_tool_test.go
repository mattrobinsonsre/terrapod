package mcpserver

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// terrapod_run_plan_json (#1601) driven through a real MCP client and server,
// so every call below passes the SDK's validation of the tool's output against
// its schema. That validation is what broke: plan_json was a json.RawMessage,
// which the SDK reads as "null or array", so every real plan (an object) was
// refused — with the whole plan dumped into the error.

func planServer(t *testing.T, status int, body []byte) (*mcp.ClientSession, *int) {
	t.Helper()
	calls := 0
	sess := toolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		calls++
		if !strings.HasSuffix(r.URL.Path, "/json-output") {
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write(body)
	})
	return sess, &calls
}

func callPlanJSON(t *testing.T, sess *mcp.ClientSession, args map[string]any) (*mcp.CallToolResult, map[string]any) {
	t.Helper()
	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "terrapod_run_plan_json", Arguments: args,
	})
	if err != nil {
		// A schema mismatch surfaces here, as a protocol error.
		t.Fatalf("CallTool: %.500v", err)
	}
	if res.IsError {
		return res, nil
	}
	var out map[string]any
	if err := json.Unmarshal([]byte(resultText(t, res)), &out); err != nil {
		t.Fatalf("result is not JSON: %v", err)
	}
	return res, out
}

func TestPlanJSONToolDefaultViewIsCompact(t *testing.T) {
	sess, _ := planServer(t, http.StatusOK, loadPlanFixture(t))
	res, out := callPlanJSON(t, sess, map[string]any{"run_id": "run-aaaa"})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	if res.StructuredContent == nil {
		t.Error("no structured content")
	}
	if out["view"] != "changes" {
		t.Errorf("view = %v, want changes", out["view"])
	}
	if s := out["summary"].(map[string]any); s["add"] != 3.0 || s["change"] != 2.0 || s["destroy"] != 2.0 {
		t.Errorf("summary = %v", s)
	}
	if n := len(out["changes"].([]any)); n != 6 || out["matched"] != 6.0 {
		t.Errorf("got %d changes, matched %v; want 6 of 6", n, out["matched"])
	}
	text := resultText(t, res)
	if i := strings.Index(text, "s3cr3t"); i >= 0 {
		t.Errorf("a sensitive value leaked into the default view: …%s…", text[max(i-120, 0):min(i+40, len(text))])
	}
	// The whole fixture is ~8 KB; the compact view must be well under it.
	if len(text) > 4096 {
		t.Errorf("default view is %d bytes, want a few KB at most", len(text))
	}
}

func TestPlanJSONToolFiltersAndPages(t *testing.T) {
	sess, _ := planServer(t, http.StatusOK, loadPlanFixture(t))

	_, out := callPlanJSON(t, sess, map[string]any{"run_id": "run-aaaa", "actions": []string{"replace"}})
	changes := out["changes"].([]any)
	if len(changes) != 1 || changes[0].(map[string]any)["address"] != "terraform_data.replaced" {
		t.Errorf("replace filter returned %v", changes)
	}

	_, out = callPlanJSON(t, sess, map[string]any{"run_id": "run-aaaa", "limit": 4})
	if len(out["changes"].([]any)) != 4 || out["truncated"] != true {
		t.Errorf("limit 4: got %d changes, truncated=%v", len(out["changes"].([]any)), out["truncated"])
	}
	_, out = callPlanJSON(t, sess, map[string]any{"run_id": "run-aaaa", "start": 4, "limit": 4})
	if len(out["changes"].([]any)) != 2 || out["truncated"] != false {
		t.Errorf("second page: got %d changes, truncated=%v", len(out["changes"].([]any)), out["truncated"])
	}

	_, out = callPlanJSON(t, sess, map[string]any{"run_id": "run-aaaa", "address": "nothing.here"})
	if out["matched"] != 0.0 {
		t.Errorf("a filter matching nothing should say matched=0, got %v", out["matched"])
	}
}

func TestPlanJSONToolFullViewReturnsTheDocumentAsAnObject(t *testing.T) {
	sess, _ := planServer(t, http.StatusOK, loadPlanFixture(t))
	res, out := callPlanJSON(t, sess, map[string]any{"run_id": "run-aaaa", "view": "full"})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	doc, ok := out["plan_json"].(map[string]any)
	if !ok {
		t.Fatalf("plan_json is not an object: %T", out["plan_json"])
	}
	if n := len(doc["resource_changes"].([]any)); n != 7 {
		t.Errorf("full document has %d resource_changes, want 7", n)
	}
}

func TestPlanJSONToolFullViewPagesALargeDocument(t *testing.T) {
	fixture := loadPlanFixture(t)
	sess, _ := planServer(t, http.StatusOK, fixture)

	var got strings.Builder
	var offset float64
	var served float64
	for i := 0; ; i++ {
		if i > 100 {
			t.Fatal("paging did not terminate")
		}
		_, out := callPlanJSON(t, sess, map[string]any{
			"run_id": "run-aaaa", "view": "full", "max_bytes": 1000, "offset": offset,
		})
		if out["plan_json"] != nil {
			t.Fatal("a document larger than max_bytes came back whole")
		}
		// total_bytes describes the document being SERVED, which is the
		// redacted one (GHSA-3g53-5gw3-hh42). It no longer equals the bytes
		// the API returned, and it must not: the offsets an agent pages with
		// are offsets into what it is given.
		if total, ok := out["total_bytes"].(float64); !ok || total <= 0 {
			t.Errorf("total_bytes = %v, want a positive size", out["total_bytes"])
		} else if served != 0 && total != served {
			t.Errorf("total_bytes changed between pages: %v then %v", served, total)
		} else {
			served = total
		}
		got.WriteString(out["plan_json_text"].(string))
		if out["truncated"] != true {
			break
		}
		offset = out["next_offset"].(float64)
	}
	// The pages reassemble into the served document. Compared as parsed JSON,
	// not byte-for-byte against the fixture: redaction re-serialises, so the
	// whitespace differs and the sensitive values are gone by design.
	var round map[string]any
	if err := json.Unmarshal([]byte(got.String()), &round); err != nil {
		t.Fatalf("the pages do not reassemble into valid JSON: %v", err)
	}
	if int64(len(got.String())) != int64(served) {
		t.Errorf("reassembled %d bytes, total_bytes said %v", len(got.String()), served)
	}
	var original map[string]any
	if err := json.Unmarshal(fixture, &original); err != nil {
		t.Fatalf("fixture is not JSON: %v", err)
	}
	// Same document, structurally: every top-level key survives redaction.
	for k := range original {
		if _, ok := round[k]; !ok {
			t.Errorf("redaction dropped top-level key %q", k)
		}
	}
}

func TestPlanJSONToolSaysWhenThereIsNoPlan(t *testing.T) {
	sess, _ := planServer(t, http.StatusNotFound, []byte(`{"errors":[{"status":"404","detail":"Plan JSON not found"}]}`))
	res, _ := callPlanJSON(t, sess, map[string]any{"run_id": "run-aaaa"})
	if !res.IsError || !strings.Contains(resultText(t, res), "no JSON plan is available") {
		t.Errorf("want a clear not-available error, got %s", resultText(t, res))
	}
}

func TestPlanJSONToolDoesNotEchoAnUnparseableBody(t *testing.T) {
	body := []byte("not json " + strings.Repeat("z", 5000))
	sess, _ := planServer(t, http.StatusOK, body)
	res, _ := callPlanJSON(t, sess, map[string]any{"run_id": "run-aaaa"})
	if !res.IsError {
		t.Fatal("expected an error for a body that is not a plan")
	}
	if strings.Contains(resultText(t, res), "zzzz") {
		t.Error("the error echoes the body")
	}
}

func TestPlanJSONToolRefusesBadArgumentsWithoutCallingTheAPI(t *testing.T) {
	sess, calls := planServer(t, http.StatusOK, loadPlanFixture(t))
	for _, args := range []map[string]any{
		{"run_id": ""},
		{"run_id": "run-aaaa", "view": "diff"},
		{"run_id": "run-aaaa", "actions": []string{"destroy"}},
		{"run_id": "run-aaaa", "address": `a*\`},
	} {
		res, _ := callPlanJSON(t, sess, args)
		if !res.IsError {
			t.Errorf("%v: expected an error", args)
		}
	}
	if *calls != 0 {
		t.Errorf("bad arguments reached the API %d times", *calls)
	}
}
