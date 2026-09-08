package terrapod

import (
	"context"
	"fmt"
)

// Engine describes one execution engine a deployment serves, and — the part
// that matters — what a run's internal status *means* for it.
//
// Run statuses are the platform's and never change: a run is "planning"
// whatever engine it belongs to. What that state *is* differs, because the
// engines do genuinely different things. Terraform plans and applies; Pulumi
// previews and updates; Ansible checks and runs. Without StatusPhases a client
// holding a run's Engine can only map that by convention, which is how four
// consumers end up with four slightly different answers.
//
// Everything here is a token, never prose. Display strings are translated per
// locale, so they cannot come from an API response — a client maps these
// identifiers to its own wording.
type Engine struct {
	// Name is the engine family: "terraform" today.
	Name string `json:"name"`

	// Phases are the engine's phases in the order a run performs them —
	// {"plan", "apply"} for Terraform.
	Phases []string `json:"phases"`

	// StatusPhases maps each internal run status onto the phase this engine
	// calls it, e.g. "planning" -> "plan". This is what lets a caller report a
	// Pulumi run as previewing without hard-coding a mapping it cannot see.
	StatusPhases map[string]string `json:"status-phases"`

	// DefaultExecutionBackend is the binary a workspace gets when it does not
	// choose one. For Terraform that is the tofu/terraform split — a choice
	// *within* the engine, not a different engine.
	DefaultExecutionBackend string `json:"default-execution-backend"`

	// Vocabulary names the message-catalogue namespace holding this engine's
	// display words. A namespace, not the words: see the note above.
	Vocabulary string `json:"vocabulary"`
}

// PhaseFor returns the engine's phase for an internal run status, and whether
// the status is one this engine has a phase for.
//
// The second return distinguishes "this engine calls that state something else"
// from "this state has no phase" — a terminal status like "errored" belongs to
// no phase, and reporting it as a plan would be wrong rather than merely
// imprecise.
func (e *Engine) PhaseFor(status string) (string, bool) {
	phase, ok := e.StatusPhases[status]
	return phase, ok
}

func engineFromResource(res *Resource) *Engine {
	raw := GetMapAttr(res, "status-phases")
	phases := make(map[string]string, len(raw))
	for k, v := range raw {
		phases[k] = v
	}
	return &Engine{
		Name:                    GetStringAttr(res, "name"),
		Phases:                  GetListAttr(res, "phases"),
		StatusPhases:            phases,
		DefaultExecutionBackend: GetStringAttr(res, "default-execution-backend"),
		Vocabulary:              GetStringAttr(res, "vocabulary"),
	}
}

// ListEngines returns the engines this deployment serves.
//
// An engine gated off in this deployment is absent rather than listed and
// refused, so the result describes what can actually be run here.
func (c *Client) ListEngines(ctx context.Context) ([]Engine, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/engines")
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, err
	}
	out := make([]Engine, 0, len(resources))
	for i := range resources {
		out = append(out, *engineFromResource(&resources[i]))
	}
	return out, nil
}

// GetEngine reads one engine by name. Returns a NotFoundError for an engine
// this deployment does not serve.
func (c *Client) GetEngine(ctx context.Context, name string) (*Engine, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/engines/"+name)
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse engine response: %w", err)
	}
	return engineFromResource(res), nil
}
