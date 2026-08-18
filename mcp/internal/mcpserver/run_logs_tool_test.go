package mcpserver

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// terrapod_run_logs (#1369) driven end to end.
//
// The catalogue golden pins that the tool is registered. What it cannot pin is
// the part with actual judgement in it: which SLICE of a large log comes back.
// A run's log is the one artifact big enough to blow a context window on a
// single call, so "return the end by default, the requested window when paging"
// is the behaviour worth testing rather than the wiring.

func logCaller(t *testing.T, body string, gotQuery *string) *mcp.ClientSession {
	t.Helper()
	return toolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		if gotQuery != nil {
			*gotQuery = r.URL.Path + "?" + r.URL.RawQuery
		}
		w.Header().Set("Content-Type", "text/plain")
		_, _ = fmt.Fprint(w, body)
	})
}

func callLogs(t *testing.T, sess *mcp.ClientSession, args map[string]any) map[string]any {
	t.Helper()
	res, err := sess.CallTool(t.Context(), &mcp.CallToolParams{Name: "terrapod_run_logs", Arguments: args})
	if err != nil {
		t.Fatalf("CallTool: %v", err)
	}
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	var out map[string]any
	if err := json.Unmarshal(mustJSON(t, res.StructuredContent), &out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	return out
}

func mustJSON(t *testing.T, v any) []byte {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return b
}

func TestRunLogsReturnsTheEndOfALongLogByDefault(t *testing.T) {
	// The failure is on the last line. A tool that returned the first 16KB
	// would hand back 16KB of successful resource creation and none of the
	// reason the run failed — worse than useless, because it looks like an
	// answer.
	body := strings.Repeat("noise line that is not why it failed\n", 2000) +
		"Error: creating RDS instance: InvalidParameterValue\n"
	sess := logCaller(t, body, nil)

	out := callLogs(t, sess, map[string]any{"run_id": "run-aaaa"})

	log, _ := out["log"].(string)
	if !strings.Contains(log, "Error: creating RDS instance") {
		t.Error("the tail — and so the actual error — was not returned")
	}
	if truncated, _ := out["truncated"].(bool); !truncated {
		t.Error("truncated should be reported when output was dropped")
	}
	if off, _ := out["offset"].(float64); off <= 0 {
		t.Error("offset must say where the returned chunk starts, so a caller can page back")
	}
}

func TestRunLogsHonoursAnExplicitOffsetInsteadOfTailing(t *testing.T) {
	// Paging forward from a known offset: keep the FRONT of that window.
	// Tailing here would skip the very bytes the caller asked for.
	body := "AAAA" + strings.Repeat("B", 40000) + "ZZZZ-the-end"
	sess := logCaller(t, body, nil)

	out := callLogs(t, sess, map[string]any{"run_id": "run-aaaa", "offset": 4, "max_bytes": 100})

	log, _ := out["log"].(string)
	if strings.Contains(log, "ZZZZ-the-end") {
		t.Error("an explicit offset was tailed rather than honoured")
	}
	if off, _ := out["offset"].(float64); off != 4 {
		t.Errorf("offset should be echoed as requested, got %v", off)
	}
}

func TestRunLogsAsksTheServerForPlainText(t *testing.T) {
	// Raw terraform output is full of ANSI escapes, which are pure context
	// burn for a model.
	var q string
	sess := logCaller(t, "ok", &q)
	callLogs(t, sess, map[string]any{"run_id": "run-aaaa"})
	if !strings.Contains(q, "format=plain") {
		t.Errorf("expected ANSI-stripped output to be requested, got %q", q)
	}
}

func TestRunLogsPhaseSelectsTheCollection(t *testing.T) {
	for phase, want := range map[string]string{"plan": "/api/v2/plans/", "apply": "/api/v2/applies/"} {
		var q string
		sess := logCaller(t, "ok", &q)
		callLogs(t, sess, map[string]any{"run_id": "run-aaaa", "phase": phase})
		if !strings.Contains(q, want) {
			t.Errorf("phase %q hit %q, want %q", phase, q, want)
		}
	}
}

func TestRunLogsRejectsAnUnknownPhase(t *testing.T) {
	sess := logCaller(t, "ok", nil)
	res, err := sess.CallTool(t.Context(), &mcp.CallToolParams{
		Name: "terrapod_run_logs", Arguments: map[string]any{"run_id": "run-aaaa", "phase": "destroy"},
	})
	if err != nil {
		t.Fatalf("CallTool: %v", err)
	}
	if !res.IsError {
		t.Error("an unknown phase should be refused, not silently treated as plan")
	}
}

func TestRunLogsShortLogIsNotMarkedTruncated(t *testing.T) {
	// A run that has not reached the phase yet has little or nothing to show.
	// That is not an error and must not be dressed up as a truncated read.
	sess := logCaller(t, "Terraform will perform the following actions\n", nil)
	out := callLogs(t, sess, map[string]any{"run_id": "run-aaaa"})
	if truncated, _ := out["truncated"].(bool); truncated {
		t.Error("a short log must not report truncation")
	}
	if off, _ := out["offset"].(float64); off != 0 {
		t.Errorf("a whole-log read starts at 0, got %v", off)
	}
}

func TestRunLogsTailStartsAtALineBoundary(t *testing.T) {
	// A byte-sized tail lands mid-line. Found against a live instance: the
	// first line came back as a severed base64 fragment from a certificate,
	// which reads as corrupt output rather than as a truncation.
	body := strings.Repeat("a line of perfectly ordinary log output here\n", 500) +
		"Error: the thing that actually went wrong\n"
	sess := logCaller(t, body, nil)

	out := callLogs(t, sess, map[string]any{"run_id": "run-aaaa", "max_bytes": 200})

	log, _ := out["log"].(string)
	if !strings.HasPrefix(log, "a line of perfectly ordinary") {
		t.Errorf("tail should begin at a line start, got %q", log[:min(60, len(log))])
	}
	if !strings.Contains(log, "Error: the thing that actually went wrong") {
		t.Error("trimming to a line boundary must not drop the end of the log")
	}
}

func TestRunLogsTailWithNoNewlineIsStillReturned(t *testing.T) {
	// One enormous line with no newline in the tail window: there is no
	// boundary to advance to, and returning nothing would be far worse than
	// returning a mid-line fragment.
	sess := logCaller(t, strings.Repeat("x", 5000), nil)
	out := callLogs(t, sess, map[string]any{"run_id": "run-aaaa", "max_bytes": 100})
	if b, _ := out["bytes"].(float64); b == 0 {
		t.Error("a log with no newlines must still return its tail")
	}
}
