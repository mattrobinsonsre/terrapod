package mcpserver

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// terrapod_run_create's CONFIGURE arguments, driven end to end (#2531).
//
// The tool already takes `is_configure`, `playbook` and `limit`, and the frozen
// catalogue pins that they are in its input_schema. That is NOT the same as
// pinning that they reach the server: the schema is derived from the Go input
// struct, so deleting the three pass-through lines in the handler leaves the
// catalogue golden -- and every other test in this package -- perfectly green,
// while an agent asking for a configure silently gets an ordinary plan.
// Measured: with those lines removed the whole mcp module still passed.
//
// A plain plan is asserted in the same breath, because the dangerous half of
// the omission is the one that CANNOT be seen in the configure case: a run that
// sends `is-configure` when nobody asked for it would change real hosts.

func decodeRunAttrs(t *testing.T, body []byte) map[string]any {
	t.Helper()
	var doc struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		t.Fatalf("decoding the request body %q: %v", body, err)
	}
	return doc.Data.Attributes
}

// runCreateCaller answers a run-create with a created configure run and hands
// back whatever attributes the tool sent.
func runCreateCaller(t *testing.T, gotAttrs *map[string]any) *mcp.ClientSession {
	t.Helper()
	return toolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		*gotAttrs = decodeRunAttrs(t, body)
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"data":{"id":"run-cfg1","type":"runs","attributes":` +
			`{"status":"pending","is-configure":true,"plan-only":false}}}`))
	})
}

func TestRunCreateSendsTheConfigureArguments(t *testing.T) {
	var attrs map[string]any
	sess := runCreateCaller(t, &attrs)

	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "terrapod_run_create",
		Arguments: map[string]any{
			"workspace_id": "ws-aaaa",
			// A configure has no plan-only form, so the caller must say so
			// explicitly -- the tool's default of true is deliberately NOT
			// flipped for a configure, because a host-changing run must never
			// be what an omitted argument produces.
			"plan_only":    false,
			"is_configure": true,
			"playbook":     "site.yml",
			"limit":        "web",
		},
	})
	if err != nil {
		t.Fatalf("CallTool: %v", err)
	}
	if res.IsError {
		t.Fatalf("tool reported an error: %s", resultText(t, res))
	}

	if attrs["is-configure"] != true {
		t.Errorf("the run was not queued as a configure: is-configure=%v", attrs["is-configure"])
	}
	if attrs["playbook"] != "site.yml" {
		t.Errorf("playbook did not reach the server: %v", attrs["playbook"])
	}
	if attrs["limit"] != "web" {
		t.Errorf("limit did not reach the server: %v", attrs["limit"])
	}
	// The limit is what keeps a configure off a whole fleet, so a dropped one
	// is worse than a dropped playbook: it widens the blast radius silently.
	if attrs["plan-only"] != false {
		t.Errorf("plan-only should have been sent as false, got %v", attrs["plan-only"])
	}
}

func TestRunCreateDoesNotSendConfigureArgumentsForAnOrdinaryPlan(t *testing.T) {
	var attrs map[string]any
	sess := runCreateCaller(t, &attrs)

	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name:      "terrapod_run_create",
		Arguments: map[string]any{"workspace_id": "ws-aaaa"},
	})
	if err != nil {
		t.Fatalf("CallTool: %v", err)
	}
	if res.IsError {
		t.Fatalf("tool reported an error: %s", resultText(t, res))
	}

	if attrs["is-configure"] == true {
		t.Errorf("a plain plan must not be queued as a configure")
	}
	for _, k := range []string{"playbook", "limit"} {
		if _, present := attrs[k]; present {
			t.Errorf("%q must be absent from a plan's request, not sent empty: the server "+
				"refuses it on a run that is not a configure, so sending it would 422 "+
				"every ordinary plan", k)
		}
	}
	// Omitted means plan-only, which is the tool's documented safe default.
	if attrs["plan-only"] != true {
		t.Errorf("an omitted plan_only must send plan-only=true, got %v", attrs["plan-only"])
	}
}
