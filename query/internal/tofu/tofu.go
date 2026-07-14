// Package tofu is a thin orchestration wrapper around the OpenTofu CLI.
//
// terrapod-query deliberately drives the `tofu` binary rather than speaking the
// provider plugin protocol directly. OpenTofu already exposes everything the
// discovery engine needs — schema introspection (`providers schema -json`),
// data-source execution (a `data` block plus `output -json`), and config
// generation (`plan -generate-config-out`) — so reimplementing a plugin client
// would rebuild a wheel tofu already turns. This package is that orchestration
// seam: it shells tofu in a caller-supplied working directory and returns the
// raw bytes for the caller to parse.
package tofu

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
)

// Runner shells a specific `tofu` binary in a specific working directory. The
// working directory is expected to already contain the ephemeral configuration
// (and, for schema/query, to have been through `Init`) the caller wants to act
// on. Runner holds no run state — it is a stateless launcher.
type Runner struct {
	// Bin is the path to the tofu executable. Empty means "tofu" on PATH.
	Bin string
	// Dir is the working directory tofu runs in.
	Dir string
}

// bin resolves the executable name, defaulting to "tofu" on PATH.
func (r *Runner) bin() string {
	if r.Bin != "" {
		return r.Bin
	}
	return "tofu"
}

// run executes tofu with the given arguments and returns stdout. On failure it
// returns an error that carries stderr, since tofu writes diagnostics there.
func (r *Runner) run(ctx context.Context, args ...string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, r.bin(), args...)
	cmd.Dir = r.Dir
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return nil, fmt.Errorf("tofu %v: %w: %s", args, err, stderr.String())
	}
	return stdout.Bytes(), nil
}

// Init runs `tofu init` so the provider plugins are installed and the schema is
// resolvable. Discovery is read-only, so no backend is configured; init only
// needs the providers.
func (r *Runner) Init(ctx context.Context) error {
	_, err := r.run(ctx, "init", "-input=false", "-no-color")
	return err
}

// ProvidersSchema returns the raw `tofu providers schema -json` output. The
// working directory must have been initialised first (Init) so the providers
// are present. The bytes are the machine-readable schema of every provider,
// resource, and data source — the input the schema package parses.
func (r *Runner) ProvidersSchema(ctx context.Context) ([]byte, error) {
	return r.run(ctx, "providers", "schema", "-json")
}

// Apply reads the configuration in the working directory and writes the result
// to local state. terrapod-query only ever applies a data-source-only
// configuration (a `data` block plus an `output`), which issues provider
// read/`Describe` calls and makes NO changes to infrastructure — it is the
// tofu-native way to force the data source to be read and its result to be
// materialised for `output -json`. There are no managed `resource` blocks in a
// discovery configuration, so nothing can be created, changed, or destroyed.
func (r *Runner) Apply(ctx context.Context) error {
	_, err := r.run(ctx, "apply", "-auto-approve", "-input=false", "-no-color")
	return err
}

// OutputJSON returns the JSON-encoded value of a named output via
// `tofu output -json <name>`. The value is the data source's attributes — the
// structured result the query package parses (no text scraping).
func (r *Runner) OutputJSON(ctx context.Context, name string) ([]byte, error) {
	return r.run(ctx, "output", "-json", name)
}
