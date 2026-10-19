package terrapod

import (
	"encoding/json"
	"testing"
)

// The create/update requests are hand-rolled map builders, NOT struct
// marshalling — the `json:"..."` tags on the request structs are never used by
// them. So adding a field to a request struct does nothing until a line is
// added to the builder, and nothing fails: the field is simply dropped on the
// floor, the server keeps its default, and a provider attribute reads back
// wrong.
//
// That is exactly how `debug-mode` and `ai-policy-mode` shipped inert in
// v1.8.0. `terrapod_workspace.debug_mode = true` sent no such key, read back
// false, and Terraform aborted the apply with "Provider produced inconsistent
// result after apply". The exported-surface golden passed throughout, because
// a golden records that a FIELD exists, not that it reaches the wire.
//
// These assert the wire, which is the only thing that can catch it.

func TestCreateSendsEverySettingItAccepts(t *testing.T) {
	debug := true
	forkPlans := true
	attrs := workspaceCreateAttrs(CreateWorkspaceRequest{
		Name:             "smoke",
		DebugMode:        &debug,
		AllowForkPRPlans: &forkPlans,
		AIPolicyMode:     "enabled",
		AISummaryMode:    "disabled",
		AISummaryContext: "ctx",
	})

	for key, want := range map[string]any{
		"debug-mode":          true,
		"allow-fork-pr-plans": true,
		"ai-policy-mode":      "enabled",
		"ai-summary-mode":     "disabled",
		"ai-summary-context":  "ctx",
	} {
		got, ok := attrs[key]
		if !ok {
			t.Errorf("create dropped %q entirely — the field never reaches the server", key)
			continue
		}
		if got != want {
			t.Errorf("create sent %q = %v, want %v", key, got, want)
		}
	}
}

func TestUpdateSendsEverySettingItAccepts(t *testing.T) {
	debug := false
	forkPlans := false
	attrs := workspaceUpdateAttrs(UpdateWorkspaceRequest{
		DebugMode:        &debug,
		AllowForkPRPlans: &forkPlans,
		AIPolicyMode:     "disabled",
	})

	// false is the interesting case for a *bool: a naive `if req.DebugMode`
	// guard would drop it and make "turn debug mode off" a silent no-op.
	if got, ok := attrs["debug-mode"]; !ok || got != false {
		t.Errorf("update dropped debug-mode=false (got %v, present=%v)", got, ok)
	}
	// Dropping this one silently leaves fork pull requests planning on a
	// workspace whose operator has just asked for them to stop.
	if got, ok := attrs["allow-fork-pr-plans"]; !ok || got != false {
		t.Errorf("update dropped allow-fork-pr-plans=false (got %v, present=%v)", got, ok)
	}
	if got, ok := attrs["ai-policy-mode"]; !ok || got != "disabled" {
		t.Errorf("update dropped ai-policy-mode (got %v, present=%v)", got, ok)
	}
}

func TestUnsetSettingsAreOmittedSoPatchLeavesThemAlone(t *testing.T) {
	attrs := workspaceUpdateAttrs(UpdateWorkspaceRequest{Name: "smoke"})

	for _, key := range []string{"debug-mode", "allow-fork-pr-plans", "ai-policy-mode"} {
		if _, present := attrs[key]; present {
			t.Errorf("update sent %q when the caller set nothing — PATCH would "+
				"overwrite a value the caller never mentioned", key)
		}
	}
}

func TestWorkspaceDecodesTheSettingsItSends(t *testing.T) {
	// A field the SDK writes but cannot read back is drift nothing detects:
	// the provider's Read leaves the prior state in place and plan shows no
	// change. `ai-policy-mode` shipped write-only for exactly this reason.
	res := &Resource{
		ID:   "ws-1",
		Type: "workspaces",
		Attributes: map[string]json.RawMessage{
			"name":                json.RawMessage(`"smoke"`),
			"debug-mode":          json.RawMessage(`true`),
			"allow-fork-pr-plans": json.RawMessage(`true`),
			"ai-policy-mode":      json.RawMessage(`"enabled"`),
		},
	}
	ws := workspaceFromResource(res)

	if !ws.DebugMode {
		t.Error("debug-mode did not decode")
	}
	if !ws.AllowForkPRPlans {
		t.Error("allow-fork-pr-plans did not decode")
	}
	if ws.AIPolicyMode != "enabled" {
		t.Errorf("ai-policy-mode decoded as %q, want %q", ws.AIPolicyMode, "enabled")
	}
}

// The Ansible execution environment's resources (#2154). Same wire trap as the
// settings above, and one more besides: the RUN carries three fields nothing
// sends -- they are read-only, populated by the server -- so for those the
// failure mode is the mirror of the one this file is about. A declared field
// that the parser never assigns is permanently nil and no fixture without it
// will say so.

func TestCreateAndUpdateSendTheExecutionEnvironmentResources(t *testing.T) {
	for name, attrs := range map[string]map[string]any{
		"create": workspaceCreateAttrs(CreateWorkspaceRequest{
			Name:             "smoke",
			EEResourceCPU:    "2",
			EEResourceMemory: "4Gi",
		}),
		"update": workspaceUpdateAttrs(UpdateWorkspaceRequest{
			EEResourceCPU:    "2",
			EEResourceMemory: "4Gi",
		}),
	} {
		for key, want := range map[string]any{
			"ee-resource-cpu":    "2",
			"ee-resource-memory": "4Gi",
		} {
			got, ok := attrs[key]
			if !ok {
				t.Errorf("%s dropped %q entirely — sizing the EE would be a silent no-op", name, key)
				continue
			}
			if got != want {
				t.Errorf("%s sent %q = %v, want %v", name, key, got, want)
			}
		}
	}
}

func TestWorkspaceDecodesTheExecutionEnvironmentResources(t *testing.T) {
	ws := workspaceFromResource(&Resource{
		ID:   "ws-1",
		Type: "workspaces",
		Attributes: map[string]json.RawMessage{
			"name":               json.RawMessage(`"smoke"`),
			"ee-resource-cpu":    json.RawMessage(`"2"`),
			"ee-resource-memory": json.RawMessage(`"4Gi"`),
		},
	})
	if ws.EEResourceCPU != "2" {
		t.Errorf("EEResourceCPU = %q, want %q", ws.EEResourceCPU, "2")
	}
	if ws.EEResourceMemory != "4Gi" {
		t.Errorf("EEResourceMemory = %q, want %q", ws.EEResourceMemory, "4Gi")
	}
}

func TestRunDecodesWhichExecutionEnvironmentItUsed(t *testing.T) {
	// The image reference matters most: the built-in EE tags MOVE as a line is
	// rebuilt for upstream patches, so a run that does not record its own
	// reference cannot be identified after the fact.
	run := runFromResource(&Resource{
		ID:   "run-1",
		Type: "runs",
		Attributes: map[string]json.RawMessage{
			"status":             json.RawMessage(`"applied"`),
			"ee-image":           json.RawMessage(`"ghcr.io/x/terrapod-ee:2.16"`),
			"ee-resource-cpu":    json.RawMessage(`"500m"`),
			"ee-resource-memory": json.RawMessage(`"1Gi"`),
		},
	})
	for _, c := range []struct{ name, got, want string }{
		{"EEImage", run.EEImage, "ghcr.io/x/terrapod-ee:2.16"},
		{"EEResourceCPU", run.EEResourceCPU, "500m"},
		{"EEResourceMemory", run.EEResourceMemory, "1Gi"},
	} {
		if c.got != c.want {
			t.Errorf("%s = %q, want %q — declared and populated from nowhere", c.name, c.got, c.want)
		}
	}
}

func TestAPlanRunCarriesNoExecutionEnvironment(t *testing.T) {
	// The pair to the test above: every plan and apply leaves all three empty,
	// which is what renders no sidecar at all. Asserted so "a terraform-only
	// deployment pays nothing" is checked on this side of the wire too.
	run := runFromResource(&Resource{
		ID:         "run-2",
		Type:       "runs",
		Attributes: map[string]json.RawMessage{"status": json.RawMessage(`"planned"`)},
	})
	if run.EEImage != "" || run.EEResourceCPU != "" || run.EEResourceMemory != "" {
		t.Errorf("a plan run carries EE fields: %q %q %q",
			run.EEImage, run.EEResourceCPU, run.EEResourceMemory)
	}
}
