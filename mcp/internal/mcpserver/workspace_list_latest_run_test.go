package mcpserver

import (
	"io"
	"net/http"
	"strings"
	"testing"
)

// An agent orienting itself asks "which workspaces have something to act on".
// `terrapod_workspace_list` projects to a reduced summary, and that summary had
// never carried `latest-run` -- so the only way to answer was to `get` every
// workspace in turn, paying per workspace for a question about the fleet
// (#2630). These hold the field in the projection, since a struct field is
// exactly the kind of thing a later tidy drops without anything noticing.

const wsWithRun = `{"data":[{"type":"workspaces","id":"ws-1","attributes":{` +
	`"name":"infra","engine":"terraform","execution-mode":"agent",` +
	`"latest-run":{"id":"run-9","status":"planned","plan-only":false,` +
	`"created-at":"2026-01-02T03:04:05Z","run-kind":"plan_apply","is-destroy":true,` +
	`"phase-started-at":{"planning-at":"2026-01-02T03:04:06Z"},` +
	`"actions":{"is-confirmable":true,"is-discardable":true,"is-cancelable":false,"is-retryable":false}}}}]}`

const wsWithoutRun = `{"data":[{"type":"workspaces","id":"ws-2","attributes":{` +
	`"name":"fresh","engine":"terraform","execution-mode":"agent"}}]}`

func listOne(t *testing.T, body string) map[string]any {
	t.Helper()
	sess := toolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = io.WriteString(w, body)
	})
	out := callTool(t, sess, "terrapod_workspace_list", map[string]any{})
	list, ok := out["workspaces"].([]any)
	if !ok || len(list) != 1 {
		t.Fatalf("expected one workspace, got %#v", out["workspaces"])
	}
	ws, ok := list[0].(map[string]any)
	if !ok {
		t.Fatalf("workspace is not an object: %#v", list[0])
	}
	return ws
}

func TestWorkspaceListCarriesTheLatestRun(t *testing.T) {
	ws := listOne(t, wsWithRun)
	run, ok := ws["latest_run"].(map[string]any)
	if !ok {
		t.Fatalf("the summary dropped `latest_run`; an agent must `get` each workspace to see run state: %#v", ws)
	}
	if run["status"] != "planned" || run["run_kind"] != "plan_apply" {
		t.Fatalf("latest_run lost its status or kind: %#v", run)
	}
	// `is-destroy` decides which capability an apply needs, and neither
	// `run_kind` nor `plan_only` can say it -- a destroy reports `plan_apply`.
	if run["is_destroy"] != true {
		t.Fatalf("latest_run lost `is_destroy`, so a destroy reads as an ordinary run: %#v", run)
	}
	// The actions block is the point: it is what says whether there is
	// anything to do, without a second call per workspace.
	acts, ok := run["actions"].(map[string]any)
	if !ok || acts["is_confirmable"] != true {
		t.Fatalf("latest_run lost the run's own actions: %#v", run)
	}
}

func TestWorkspaceListOmitsTheLatestRunWhenThereHasNeverBeenOne(t *testing.T) {
	// Absent, not an empty object. An empty one reads as a run whose status is
	// "" and whose every action is false, which is a different and wrong claim.
	ws := listOne(t, wsWithoutRun)
	if _, present := ws["latest_run"]; present {
		t.Fatalf("a workspace that has never run reported a latest_run: %#v", ws)
	}
}

func TestTheListToolSaysItCarriesTheLatestRun(t *testing.T) {
	// The tool-level description is frozen by nothing -- `tool_catalogue.json`
	// pins `name` and `input_schema` only -- so an agent can be told the list
	// has no run state long after it gained some. This is the only thing that
	// would notice.
	desc, ok := liveToolsByName(t)["terrapod_workspace_list"]
	if !ok {
		t.Fatal("terrapod_workspace_list is not registered")
	}
	if !strings.Contains(desc, "latest_run") {
		t.Fatalf("the list tool returns `latest_run` and its description does not mention it: %s", desc)
	}
}
