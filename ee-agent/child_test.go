package main

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// fakeAnsible puts a script named like a real ansible entry point at the front
// of PATH. The agent only ever EXECS ansible — it never imports it — so a script
// is a faithful stand-in for everything the agent is responsible for.
func fakeAnsible(t *testing.T, name, script string) {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte("#!/bin/sh\n"+script+"\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
}

func TestTheTokenIsScrubbedFromTheChildEnvironment(t *testing.T) {
	// The property is a NEGATIVE — that something is absent — which is exactly
	// what passes by accident, so it is asserted from the child's own view of
	// its environment rather than from the slice the agent built.
	fakeAnsible(t, "ansible-playbook", `env`)
	t.Setenv(tokenEnv, "super-secret-value")

	var out bytes.Buffer
	r := &runner{}
	env := scrubbed(os.Environ(), nil, tokenEnv)
	rc, err := r.start("ansible-playbook", nil, env, "", &out)
	if err != nil || rc != 0 {
		t.Fatalf("rc=%d err=%v out=%s", rc, err, out.String())
	}
	if strings.Contains(out.String(), "super-secret-value") {
		t.Fatal("the child could read the agent token out of its own environment")
	}
	if !strings.Contains(out.String(), "PATH=") {
		t.Fatal("the child got no environment at all, so the scrub proves nothing")
	}
}

func TestExtraEnvReachesTheChild(t *testing.T) {
	// The mirror of the test above: if nothing reached the child, the scrub
	// assertion would be vacuous.
	fakeAnsible(t, "ansible-playbook", `env`)
	var out bytes.Buffer
	r := &runner{}
	rc, err := r.start("ansible-playbook", nil,
		scrubbed(os.Environ(), []string{"ANSIBLE_LOCAL_TEMP=/tp/tmp/local"}, tokenEnv), "", &out)
	if err != nil || rc != 0 {
		t.Fatalf("rc=%d err=%v", rc, err)
	}
	if !strings.Contains(out.String(), "ANSIBLE_LOCAL_TEMP=/tp/tmp/local") {
		t.Fatal("caller-supplied env did not reach the child")
	}
}

func TestOnlyAnsibleEntryPointsRun(t *testing.T) {
	// An allowlist, not arbitrary argv: a generic exec endpoint is a remote
	// shell, and loopback is shared with the operator's own playbook.
	fakeAnsible(t, "sh", `echo pwned`)
	var out bytes.Buffer
	r := &runner{}
	rc, err := r.start("sh", []string{"-c", "echo pwned"}, os.Environ(), "", &out)
	if err == nil {
		t.Fatal("the agent ran a binary that is not an ansible entry point")
	}
	if rc != 126 {
		t.Fatalf("want rc 126 for a refused binary, got %d", rc)
	}
	if strings.Contains(out.String(), "pwned") {
		t.Fatal("the refused binary produced output, so it ran")
	}
}

func TestAMissingEntryPointIsDistinguishableFromARefusedOne(t *testing.T) {
	// 127 vs 126 matters: "your EE does not have this" and "the agent will not
	// run this" are different operator problems with different fixes.
	r := &runner{}
	t.Setenv("PATH", t.TempDir())
	rc, err := r.start("ansible-playbook", nil, os.Environ(), "", &bytes.Buffer{})
	if err == nil {
		t.Fatal("expected an error for an absent binary")
	}
	if rc != 127 {
		t.Fatalf("want rc 127 for an absent binary, got %d", rc)
	}
	if !strings.Contains(err.Error(), "not on PATH in this execution environment") {
		t.Fatalf("the error should name the EE as the thing that is missing it: %v", err)
	}
}

func TestANonZeroExitIsReportedNotWrapped(t *testing.T) {
	// ansible's exit code is its verdict and the caller needs it; wrapping it in
	// an error would lose the number.
	fakeAnsible(t, "ansible-playbook", `exit 4`)
	r := &runner{}
	rc, err := r.start("ansible-playbook", nil, os.Environ(), "", &bytes.Buffer{})
	if rc != 4 {
		t.Fatalf("want rc 4, got %d", rc)
	}
	if err != nil {
		t.Fatalf("a non-zero ansible exit is not an agent error: %v", err)
	}
}

func TestCancelSignalsTheRunningChild(t *testing.T) {
	// The proof version of this agent never assigned the child, so /cancel could
	// never find it — cancellation was asserted in a comment and never
	// exercised. This is that test.
	fakeAnsible(t, "ansible-playbook", `trap 'echo interrupted; exit 99' INT; sleep 30`)
	r := &runner{}
	var out bytes.Buffer
	done := make(chan int, 1)
	go func() {
		rc, _ := r.start("ansible-playbook", nil, os.Environ(), "", &out)
		done <- rc
	}()

	deadline := time.Now().Add(5 * time.Second)
	for !r.running() && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if !r.running() {
		t.Fatal("the child never registered, so cancel has nothing to find")
	}
	if !r.cancel() {
		t.Fatal("cancel reported nothing to signal while a child was running")
	}

	select {
	case rc := <-done:
		if rc == 0 {
			t.Fatalf("the child exited cleanly, so it was not signalled (out=%q)", out.String())
		}
	case <-time.After(10 * time.Second):
		t.Fatal("the child outlived cancellation")
	}
}

func TestCancelIsHonestWhenThereIsNothingToCancel(t *testing.T) {
	// So the caller never reports having stopped a run that had already finished.
	r := &runner{}
	if r.cancel() {
		t.Fatal("cancel claimed to signal a child that does not exist")
	}
}

func TestOneChildAtATime(t *testing.T) {
	fakeAnsible(t, "ansible-playbook", `sleep 5`)
	r := &runner{}
	go func() { _, _ = r.start("ansible-playbook", nil, os.Environ(), "", &bytes.Buffer{}) }()
	deadline := time.Now().Add(5 * time.Second)
	for !r.running() && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	_, err := r.start("ansible-playbook", nil, os.Environ(), "", &bytes.Buffer{})
	if err == nil {
		t.Fatal("a second child started while one was already running")
	}
	r.cancel()
}

func TestTheChildGetsBlockingStdio(t *testing.T) {
	// ansible refuses to start at all on a non-blocking descriptor, exiting
	// before it does any work and naming only the descriptor
	// ("Ansible requires blocking IO on stdin/stdout/stderr"). The agent owns
	// those descriptors, so this is the agent's property to hold.
	//
	// Asserted the way the shell can see it: a child that can read stdin to EOF
	// and write without EAGAIN has blocking descriptors.
	fakeAnsible(t, "ansible-playbook", `cat >/dev/null; echo stdio-ok`)
	var out bytes.Buffer
	r := &runner{}
	rc, err := r.start("ansible-playbook", nil, os.Environ(), "", &out)
	if err != nil || rc != 0 {
		t.Fatalf("rc=%d err=%v out=%q", rc, err, out.String())
	}
	if !strings.Contains(out.String(), "stdio-ok") {
		t.Fatalf("the child could not use its descriptors: %q", out.String())
	}
}

func TestOutputStreamsRatherThanBuffering(t *testing.T) {
	// The orchestrator tees this into the run log and the secret redactor runs
	// over it, so a configure that showed nothing until it finished would be a
	// defect in both.
	fakeAnsible(t, "ansible-playbook", `echo first; sleep 1; echo second`)
	seen := make(chan string, 4)
	r := &runner{}
	go func() {
		_, _ = r.start("ansible-playbook", nil, os.Environ(), "", writerFunc(func(p []byte) (int, error) {
			seen <- string(p)
			return len(p), nil
		}))
	}()
	select {
	case chunk := <-seen:
		if !strings.Contains(chunk, "first") {
			t.Fatalf("first chunk was %q", chunk)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("nothing arrived before the child finished: output is buffered")
	}
}

type writerFunc func([]byte) (int, error)

func (f writerFunc) Write(p []byte) (int, error) { return f(p) }

func TestScrubbedRemovesOnlyTheToken(t *testing.T) {
	parent := []string{"PATH=/usr/bin", tokenEnv + "=secret", "HOME=/home/ansible", "TP_OTHER=keep"}
	got := scrubbed(parent, []string{"EXTRA=1"}, tokenEnv)
	joined := strings.Join(got, " ")
	for _, want := range []string{"PATH=/usr/bin", "HOME=/home/ansible", "TP_OTHER=keep", "EXTRA=1"} {
		if !strings.Contains(joined, want) {
			t.Errorf("scrub removed %q, which it should not have", want)
		}
	}
	if strings.Contains(joined, "secret") {
		t.Error("the token survived the scrub")
	}
	if len(got) != len(parent) {
		t.Errorf("expected %d entries (one removed, one added), got %d: %v", len(parent), len(got), got)
	}
}

func TestFakeAnsibleIsActuallyBeingUsed(t *testing.T) {
	// Guards every test above: if the fake were not on PATH, a real ansible (or
	// nothing) would be exercised and the suite would be measuring something else.
	fakeAnsible(t, "ansible-playbook", `echo FAKE-MARKER-`+fmt.Sprint(os.Getpid()))
	var out bytes.Buffer
	r := &runner{}
	if _, err := r.start("ansible-playbook", nil, os.Environ(), "", &out); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "FAKE-MARKER-") {
		t.Fatalf("the fake was not what ran: %q", out.String())
	}
}
