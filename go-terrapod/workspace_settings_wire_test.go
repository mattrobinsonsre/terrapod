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
	attrs := workspaceCreateAttrs(CreateWorkspaceRequest{
		Name:             "smoke",
		DebugMode:        &debug,
		AIPolicyMode:     "enabled",
		AISummaryMode:    "disabled",
		AISummaryContext: "ctx",
	})

	for key, want := range map[string]any{
		"debug-mode":         true,
		"ai-policy-mode":     "enabled",
		"ai-summary-mode":    "disabled",
		"ai-summary-context": "ctx",
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
	attrs := workspaceUpdateAttrs(UpdateWorkspaceRequest{
		DebugMode:    &debug,
		AIPolicyMode: "disabled",
	})

	// false is the interesting case for a *bool: a naive `if req.DebugMode`
	// guard would drop it and make "turn debug mode off" a silent no-op.
	if got, ok := attrs["debug-mode"]; !ok || got != false {
		t.Errorf("update dropped debug-mode=false (got %v, present=%v)", got, ok)
	}
	if got, ok := attrs["ai-policy-mode"]; !ok || got != "disabled" {
		t.Errorf("update dropped ai-policy-mode (got %v, present=%v)", got, ok)
	}
}

func TestUnsetSettingsAreOmittedSoPatchLeavesThemAlone(t *testing.T) {
	attrs := workspaceUpdateAttrs(UpdateWorkspaceRequest{Name: "smoke"})

	for _, key := range []string{"debug-mode", "ai-policy-mode"} {
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
			"name":           json.RawMessage(`"smoke"`),
			"debug-mode":     json.RawMessage(`true`),
			"ai-policy-mode": json.RawMessage(`"enabled"`),
		},
	}
	ws := workspaceFromResource(res)

	if !ws.DebugMode {
		t.Error("debug-mode did not decode")
	}
	if ws.AIPolicyMode != "enabled" {
		t.Errorf("ai-policy-mode decoded as %q, want %q", ws.AIPolicyMode, "enabled")
	}
}
