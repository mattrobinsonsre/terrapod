package main

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"sync"
	"syscall"
	"time"
)

// allowed is every binary the agent will run. An allowlist rather than arbitrary
// argv, because a generic exec endpoint is a remote shell and "it is only
// reachable on loopback" is not a reason to build one — the operator's own
// playbook shares that loopback.
//
// ansible-galaxy is here because collection and role install must be performed
// by the SAME ansible-core that will run the content: `requires_ansible` is not
// enforced at install time (measured — a collection declaring ">=2.20.0"
// installs on 2.16.19 at exit 0 with no warning), so installing with one core
// and running with another produces a set that fails mid-play.
var allowed = map[string]bool{
	"ansible":           true,
	"ansible-playbook":  true,
	"ansible-galaxy":    true,
	"ansible-inventory": true,
	"ansible-config":    true,
	"ansible-vault":     true,
}

// childGrace is how long a cancelled or terminating child gets after SIGINT
// before it is killed. It mirrors the runner's own contract: SIGTERM forwards
// ONE SIGINT, because a second makes ansible abort ungracefully.
const childGrace = 25 * time.Second

// runner owns the single child process. A configure is sequential, so one at a
// time is the whole concurrency model — and it is what lets /cancel name "the"
// child without an identifier.
type runner struct {
	mu    sync.Mutex
	child *exec.Cmd
}

// scrubbed returns the environment a child should see: everything the agent has,
// minus the agent's own token, plus whatever the caller added.
//
// Separated out and exported to the tests because the property is a negative —
// that something is ABSENT — and a negative is exactly what passes by accident.
func scrubbed(parent []string, extra []string, tokenKey string) []string {
	child := make([]string, 0, len(parent)+len(extra))
	for _, kv := range parent {
		if strings.HasPrefix(kv, tokenKey+"=") {
			continue
		}
		child = append(child, kv)
	}
	return append(child, extra...)
}

// start launches an allowlisted binary and streams its combined output to sink.
//
// Streaming rather than CombinedOutput() for two reasons that are not about
// taste: the orchestrator tees this into the run log, so a buffered child means
// a configure shows nothing until it finishes; and the secret redactor runs over
// the stream, so it needs the bytes as they arrive.
//
// The child gets a real OS pipe, which is BLOCKING on its end. That matters:
// ansible refuses to start at all on a non-blocking descriptor
// ("ERROR: Ansible requires blocking IO on stdin/stdout/stderr"), exiting before
// it does any work and naming only the descriptor. The agent owns those
// descriptors, so this is the agent's problem to get right.
func (r *runner) start(bin string, args []string, env []string, dir string, sink io.Writer) (int, error) {
	if !allowed[bin] {
		return 126, fmt.Errorf("%q is not an ansible entry point this agent will run", bin)
	}
	path, err := exec.LookPath(bin)
	if err != nil {
		return 127, fmt.Errorf("%s is not on PATH in this execution environment: %w", bin, err)
	}

	// G204 is the agent's entire purpose, and the mitigation is directly above:
	// `bin` is checked against `allowed` before we get here, and `path` is
	// `exec.LookPath` of that allowlisted name rather than a caller-supplied
	// path. Args are the caller's by design -- the orchestrator chooses the
	// playbook and the limit, which is what it is for.
	//nolint:gosec // G204: allowlisted entry point, resolved via LookPath.
	cmd := exec.Command(path, args...)
	cmd.Env = env
	cmd.Dir = dir
	// Stdin must exist and be blocking; /dev/null rather than nil so ansible
	// never sees a closed or non-blocking descriptor.
	devnull, err := os.Open(os.DevNull)
	if err != nil {
		return 1, fmt.Errorf("opening %s: %w", os.DevNull, err)
	}
	defer func() { _ = devnull.Close() }()
	cmd.Stdin = devnull

	pipe, err := cmd.StdoutPipe()
	if err != nil {
		return 1, fmt.Errorf("stdout pipe: %w", err)
	}
	cmd.Stderr = cmd.Stdout

	// Its own process group, so a signal reaches ansible's children too —
	// ansible forks per host, and signalling only the parent leaves them.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

	r.mu.Lock()
	if r.child != nil {
		r.mu.Unlock()
		return 1, fmt.Errorf("a child is already running; this agent runs one at a time")
	}
	if err := cmd.Start(); err != nil {
		r.mu.Unlock()
		return 1, fmt.Errorf("starting %s: %w", bin, err)
	}
	// Assigned BEFORE the wait, which is the whole point: the proof version of
	// this agent only ever set it to nil, so /cancel could never find the
	// process and cancellation was asserted rather than exercised.
	r.child = cmd
	r.mu.Unlock()

	_, copyErr := io.Copy(sink, pipe)
	waitErr := cmd.Wait()

	r.mu.Lock()
	r.child = nil
	r.mu.Unlock()

	rc := cmd.ProcessState.ExitCode()
	if copyErr != nil && rc == 0 {
		return rc, fmt.Errorf("reading %s output: %w", bin, copyErr)
	}
	if rc != 0 {
		// A non-zero exit is ansible's verdict, not an agent error: the caller
		// needs the code, and wrapping it in an error would lose it.
		return rc, nil
	}
	if waitErr != nil {
		return rc, waitErr
	}
	return rc, nil
}

// cancel asks the current child to stop. ONE SIGINT, then SIGKILL after the
// grace — a second SIGINT makes ansible abort ungracefully and it may then skip
// writing anything it was part way through.
//
// Reports whether there was anything to cancel, so the caller can answer
// honestly rather than claiming to have stopped a run that had already finished.
func (r *runner) cancel() bool {
	r.mu.Lock()
	cmd := r.child
	r.mu.Unlock()
	if cmd == nil || cmd.Process == nil {
		return false
	}

	// Negative pid signals the whole process group (Setpgid above).
	pgid := -cmd.Process.Pid
	_ = syscall.Kill(pgid, syscall.SIGINT)

	go func() {
		time.Sleep(childGrace)
		r.mu.Lock()
		still := r.child
		r.mu.Unlock()
		if still == cmd && cmd.Process != nil {
			_ = syscall.Kill(pgid, syscall.SIGKILL)
		}
	}()
	return true
}

// running reports whether a child is in flight, for the shutdown path.
func (r *runner) running() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.child != nil
}
