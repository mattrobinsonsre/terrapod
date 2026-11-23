package terrapod

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

// The three-state SSH host-key policy (#2592), which replaced the
// `ansible-host-key-checking` boolean (#2318) on the unreleased 2.0 line.
//
// The boolean it replaced was asserted NOWHERE in this package -- no test named
// it, on either the read or the write side -- so the rename is the moment the
// setting gets covered at all. A field that arrives through the SDK untested is
// one whose wire name nothing pins: it can be misspelled and still compile, and
// the symptom is a silently ignored setting rather than an error.
//
// What each test here pins, and why it needs its own:
//
//   - the ROUND TRIP, so the request carries the hyphenated wire name and the
//     response lands back on the field;
//   - the UNSET case, because "" means "say nothing" and the server refuses ""
//     outright -- sending it unconditionally would 422 every create that did
//     not set a policy;
//   - the middle state by name, because `accept-new` is the whole reason the
//     boolean was replaced and a fixture using only strict/off would pass
//     whether or not the SDK could carry it;
//   - the ERROR path, because the server's 422 is the authority on the value
//     set and a caller must get it rather than a zero-valued workspace.

// policyBody is a workspace response carrying one policy.
func policyBody(id, name, policy string) string {
	return minimalWorkspaceBody(id, name, map[string]any{"ssh-host-key-policy": policy})
}

// sentAttrs pulls the JSON:API attributes out of the last request the fixture saw.
func sentAttrs(t *testing.T, body []byte) map[string]any {
	t.Helper()
	var req struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &req); err != nil {
		t.Fatalf("request body: %v", err)
	}
	return req.Data.Attributes
}

func TestCreateWorkspaceSSHHostKeyPolicyRoundTrips(t *testing.T) {
	f := newWorkspaceFixtureServer(t)
	f.createHandler = func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(policyBody("ws-pol", "fleet", "accept-new")))
	}

	ws, err := f.client().CreateWorkspace(t.Context(), CreateWorkspaceRequest{
		Name:             "fleet",
		SSHHostKeyPolicy: "accept-new",
	})
	if err != nil {
		t.Fatalf("CreateWorkspace: %v", err)
	}
	if ws.SSHHostKeyPolicy != "accept-new" {
		t.Errorf("SSHHostKeyPolicy = %q, want accept-new", ws.SSHHostKeyPolicy)
	}

	attrs := sentAttrs(t, f.lastBody)
	if got := attrs["ssh-host-key-policy"]; got != "accept-new" {
		t.Errorf("request ssh-host-key-policy = %v, want accept-new", got)
	}
	// The retired spelling must not reappear beside the new one: a builder that
	// sent both would pass the assertion above while still driving the server's
	// boolean refusal.
	if _, present := attrs["ansible-host-key-checking"]; present {
		t.Errorf("the retired boolean attribute was sent: %+v", attrs)
	}
}

// TestCreateWorkspaceSSHHostKeyPolicyUnsetSendsNothing is the companion the
// round-trip test needs. The server refuses any value outside the three, "" very
// much included, so a builder that sent the field unconditionally would turn
// "take the default" into a 422 on every create that said nothing about it.
func TestCreateWorkspaceSSHHostKeyPolicyUnsetSendsNothing(t *testing.T) {
	f := newWorkspaceFixtureServer(t)
	f.createHandler = func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusCreated)
		// The server still answers with its own default, which is what makes
		// the field non-empty on read even when the caller said nothing.
		_, _ = w.Write([]byte(policyBody("ws-pol", "fleet", "strict")))
	}

	ws, err := f.client().CreateWorkspace(t.Context(), CreateWorkspaceRequest{Name: "fleet"})
	if err != nil {
		t.Fatalf("CreateWorkspace: %v", err)
	}
	if ws.SSHHostKeyPolicy != "strict" {
		t.Errorf("SSHHostKeyPolicy = %q, want the server's strict", ws.SSHHostKeyPolicy)
	}
	if _, present := sentAttrs(t, f.lastBody)["ssh-host-key-policy"]; present {
		t.Errorf("ssh-host-key-policy sent when unset: %+v", sentAttrs(t, f.lastBody))
	}
}

func TestCreateWorkspaceSSHHostKeyPolicyRefusalIsAnError(t *testing.T) {
	f := newWorkspaceFixtureServer(t)
	f.createHandler = func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(`{"errors":[{"status":"422","detail":` +
			`"ssh-host-key-policy must be 'strict', 'accept-new', or 'off'"}]}`))
	}

	ws, err := f.client().CreateWorkspace(t.Context(), CreateWorkspaceRequest{
		Name:             "fleet",
		SSHHostKeyPolicy: "paranoid",
	})
	if err == nil {
		t.Fatal("expected the server's refusal to reach the caller")
	}
	if ws != nil {
		t.Errorf("a refused create returned a workspace: %+v", ws)
	}
}

func TestUpdateWorkspaceSSHHostKeyPolicyRoundTrips(t *testing.T) {
	f := newWorkspaceFixtureServer(t)
	f.updateHandler = func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(policyBody("ws-pol", "fleet", "off")))
	}

	ws, err := f.client().UpdateWorkspace(t.Context(), "ws-pol", UpdateWorkspaceRequest{
		SSHHostKeyPolicy: "off",
	})
	if err != nil {
		t.Fatalf("UpdateWorkspace: %v", err)
	}
	if ws.SSHHostKeyPolicy != "off" {
		t.Errorf("SSHHostKeyPolicy = %q, want off", ws.SSHHostKeyPolicy)
	}
	if got := sentAttrs(t, f.lastBody)["ssh-host-key-policy"]; got != "off" {
		t.Errorf("request ssh-host-key-policy = %v, want off", got)
	}
}

// TestUpdateWorkspaceSSHHostKeyPolicyUnsetLeavesItAlone pins the PATCH half of
// the empty-string rule. The server reads an absent key as "leave the stored
// value alone", so a plain-string field must omit rather than send "" -- which
// would be refused, making every unrelated PATCH fail.
func TestUpdateWorkspaceSSHHostKeyPolicyUnsetLeavesItAlone(t *testing.T) {
	f := newWorkspaceFixtureServer(t)
	f.updateHandler = func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(policyBody("ws-pol", "renamed", "accept-new")))
	}

	if _, err := f.client().UpdateWorkspace(t.Context(), "ws-pol",
		UpdateWorkspaceRequest{Name: "renamed"}); err != nil {
		t.Fatalf("UpdateWorkspace: %v", err)
	}
	attrs := sentAttrs(t, f.lastBody)
	if _, present := attrs["ssh-host-key-policy"]; present {
		t.Errorf("ssh-host-key-policy sent when unset: %+v", attrs)
	}
}

func TestUpdateWorkspaceSSHHostKeyPolicyRefusalIsAnError(t *testing.T) {
	f := newWorkspaceFixtureServer(t)
	f.updateHandler = func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(`{"errors":[{"status":"422","detail":` +
			`"ssh-host-key-policy is not a boolean -- it replaced ansible-host-key-checking"}]}`))
	}

	ws, err := f.client().UpdateWorkspace(t.Context(), "ws-pol",
		UpdateWorkspaceRequest{SSHHostKeyPolicy: "true"})
	if err == nil {
		t.Fatal("expected the server's refusal to reach the caller")
	}
	if ws != nil {
		t.Errorf("a refused update returned a workspace: %+v", ws)
	}
}

// TestGetWorkspaceReadsEveryPolicy drives all three values through the read
// path. One value would pass whether or not the field is a plain pass-through,
// and the middle state is the one the boolean could not hold -- so a parser that
// coerced to a boolean would fail here and nowhere else.
func TestGetWorkspaceReadsEveryPolicy(t *testing.T) {
	for _, policy := range []string{"strict", "accept-new", "off"} {
		t.Run(policy, func(t *testing.T) {
			f := newWorkspaceFixtureServer(t)
			f.readHandler = func(w http.ResponseWriter, _ *http.Request) {
				_, _ = w.Write([]byte(policyBody("ws-pol", "fleet", policy)))
			}
			ws, err := f.client().GetWorkspace(t.Context(), "ws-pol")
			if err != nil {
				t.Fatalf("GetWorkspace: %v", err)
			}
			if ws.SSHHostKeyPolicy != policy {
				t.Errorf("SSHHostKeyPolicy = %q, want %q", ws.SSHHostKeyPolicy, policy)
			}
		})
	}
}

func TestGetWorkspaceSSHHostKeyPolicyNotFoundIsAnError(t *testing.T) {
	f := newWorkspaceFixtureServer(t)
	f.readHandler = func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"errors":[{"status":"404","detail":"not found"}]}`))
	}
	ws, err := f.client().GetWorkspace(t.Context(), "ws-pol")
	if err == nil {
		t.Fatal("expected an error")
	}
	if ws != nil {
		t.Errorf("a failed read returned a workspace: %+v", ws)
	}
}

// --- the run's snapshot ----------------------------------------------------

// runPolicyFixture serves one run carrying the given policy, or a status when
// the policy is empty. Its own fixture rather than the shared `runAttrs`
// payload: the point is to drive all three values plus a failure through one
// read, which a single shared constant cannot do.
func runPolicyFixture(t *testing.T, policy string, status int) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		if status != http.StatusOK {
			w.WriteHeader(status)
			_, _ = w.Write([]byte(`{"errors":[{"status":"404","detail":"run not found"}]}`))
			return
		}
		_, _ = w.Write([]byte(`{"data":{"id":"run-pol","type":"runs","attributes":{` +
			`"status":"planned","ssh-host-key-policy":"` + policy + `"}}}`))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

// TestGetRunReadsTheSnapshottedPolicy pins the run's own copy of the policy
// (#2592), which the API emits on EVERY run rather than only a configure: the
// runner cannot know the workspace's setting otherwise, and a default drifting
// between API and runner across a version skew would be invisible.
//
// All three values, for the reason the workspace read test gives -- the middle
// state is the one the boolean it replaced could not hold.
func TestGetRunReadsTheSnapshottedPolicy(t *testing.T) {
	for _, policy := range []string{"strict", "accept-new", "off"} {
		t.Run(policy, func(t *testing.T) {
			run, err := runPolicyFixture(t, policy, http.StatusOK).
				GetRun(t.Context(), "run-pol")
			if err != nil {
				t.Fatalf("GetRun: %v", err)
			}
			if run.SSHHostKeyPolicy != policy {
				t.Errorf("SSHHostKeyPolicy = %q, want %q", run.SSHHostKeyPolicy, policy)
			}
		})
	}
}

func TestGetRunSSHHostKeyPolicyNotFoundIsAnError(t *testing.T) {
	run, err := runPolicyFixture(t, "", http.StatusNotFound).GetRun(t.Context(), "run-pol")
	if err == nil {
		t.Fatal("expected an error")
	}
	if run != nil {
		t.Errorf("a failed read returned a run: %+v", run)
	}
}
