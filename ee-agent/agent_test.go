package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

func testAgent(t *testing.T, contractErr string) *httptest.Server {
	t.Helper()
	a := &agent{token: "the-token", runner: &runner{}, contractError: contractErr}
	srv := httptest.NewServer(a.handler())
	t.Cleanup(srv.Close)
	return srv
}

func post(t *testing.T, url, token string, body any) *http.Response {
	t.Helper()
	raw, _ := json.Marshal(body)
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = resp.Body.Close() })
	return resp
}

func TestHealthzIsUnauthenticated(t *testing.T) {
	// The exec probe that reads it has no way to carry a credential, and it
	// reveals only liveness.
	srv := testAgent(t, "")
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("want 200, got %d", resp.StatusCode)
	}
}

func TestHealthzReportsAContractFailureWithItsReason(t *testing.T) {
	// Reported here rather than by refusing to listen, so the reason reaches the
	// probe's output and therefore pod events — where an operator looks — instead
	// of dying in a container log while the Job sits in PodInitializing.
	srv := testAgent(t, "this execution environment is missing git on PATH")
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("want 503 for a non-conforming EE, got %d", resp.StatusCode)
	}
	body, _ := io.ReadAll(resp.Body)
	if !strings.Contains(string(body), "missing git on PATH") {
		t.Fatalf("the body must name what is missing, got %q", body)
	}
}

func TestExecRequiresTheToken(t *testing.T) {
	// Loopback is shared by every container in the pod — including the one the
	// operator's playbook runs in. It can reach this; it must not be able to use it.
	srv := testAgent(t, "")
	for _, token := range []string{"", "wrong-token", "the-token-but-longer"} {
		resp := post(t, srv.URL+"/exec", token, execRequest{Bin: "ansible-playbook"})
		if resp.StatusCode != http.StatusForbidden {
			t.Errorf("token %q got %d, want 403", token, resp.StatusCode)
		}
	}
}

func TestCancelRequiresTheToken(t *testing.T) {
	srv := testAgent(t, "")
	resp := post(t, srv.URL+"/cancel", "", nil)
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("want 403, got %d", resp.StatusCode)
	}
}

func TestExecStreamsAndReportsTheExitCode(t *testing.T) {
	fakeAnsible(t, "ansible-playbook", `echo hello-from-ee; exit 3`)
	srv := testAgent(t, "")
	resp := post(t, srv.URL+"/exec", "the-token", execRequest{Bin: "ansible-playbook"})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("want 200, got %d", resp.StatusCode)
	}
	body, _ := io.ReadAll(resp.Body)
	if !strings.Contains(string(body), "hello-from-ee") {
		t.Fatalf("the child's output did not reach the caller: %q", body)
	}
	// The exit code arrives as a trailer, because the body is streamed and the
	// code is not known until it ends.
	if got := resp.Trailer.Get("X-Terrapod-RC"); got != "3" {
		t.Fatalf("want rc 3 in the trailer, got %q", got)
	}
}

func TestExecRefusesANonAnsibleBinaryOverHTTPToo(t *testing.T) {
	// The allowlist is enforced below the handler, so this pins that the HTTP
	// path does not route around it.
	fakeAnsible(t, "sh", `echo pwned`)
	srv := testAgent(t, "")
	resp := post(t, srv.URL+"/exec", "the-token", execRequest{Bin: "sh", Args: []string{"-c", "echo pwned"}})
	body, _ := io.ReadAll(resp.Body)
	if strings.Contains(string(body), "pwned") {
		t.Fatal("a non-ansible binary ran through the HTTP surface")
	}
	if got := resp.Trailer.Get("X-Terrapod-RC"); got != "126" {
		t.Fatalf("want rc 126, got %q", got)
	}
}

func TestOnlyPostActs(t *testing.T) {
	srv := testAgent(t, "")
	for _, path := range []string{"/exec", "/cancel"} {
		resp, err := http.Get(srv.URL + path)
		if err != nil {
			t.Fatal(err)
		}
		_ = resp.Body.Close()
		if resp.StatusCode != http.StatusMethodNotAllowed {
			t.Errorf("GET %s got %d, want 405", path, resp.StatusCode)
		}
	}
}

func TestCancelAnswersWhetherThereWasAnythingToSignal(t *testing.T) {
	srv := testAgent(t, "")
	resp := post(t, srv.URL+"/cancel", "the-token", nil)
	var got map[string]bool
	_ = json.NewDecoder(resp.Body).Decode(&got)
	if got["signalled"] {
		t.Fatal("cancel claimed to signal a child that does not exist")
	}
}

func TestTheDefaultListenAddressIsLoopback(t *testing.T) {
	// ":7781" would bind every interface including the pod IP, which is routable
	// from the whole cluster. That one character is the entire isolation
	// property, so it is pinned rather than trusted to review.
	host, port, err := net.SplitHostPort(defaultListen)
	if err != nil {
		t.Fatal(err)
	}
	if port == "" {
		t.Fatal("no port in the default address")
	}
	ip := net.ParseIP(host)
	if ip == nil || !ip.IsLoopback() {
		t.Fatalf("default listen host %q is not a loopback address", host)
	}
}

func TestAListenerOnTheDefaultAddressIsNotReachableOffLoopback(t *testing.T) {
	// The behavioural half of the test above: bind the real default and confirm
	// a non-loopback local address cannot reach it. Proves the bind is what
	// denies access, not a firewall.
	ln, err := listen("127.0.0.1:0")
	if err != nil {
		t.Skipf("cannot bind loopback here: %v", err)
	}
	defer func() { _ = ln.Close() }()
	_, port, _ := net.SplitHostPort(ln.Addr().String())

	outward := outwardAddress(t)
	if outward == "" {
		t.Skip("no non-loopback address on this host to test against")
	}
	conn, err := net.DialTimeout("tcp", net.JoinHostPort(outward, port), dialTimeout)
	if err == nil {
		_ = conn.Close()
		t.Fatalf("a loopback-bound listener answered on %s", outward)
	}
}

func TestServeRefusesWithoutAToken(t *testing.T) {
	// Nothing could be authorised, so listening would create an endpoint that
	// rejects everything — which looks like a bug rather than a refusal.
	t.Setenv(tokenEnv, "")
	if rc := serve("127.0.0.1:0"); rc == 0 {
		t.Fatal("serve started with no token")
	}
}

func TestHealthcheckFailsWhenNothingIsListening(t *testing.T) {
	// The probe must fail closed: a healthcheck that passed when the agent was
	// absent would let the orchestrator start against nothing.
	if rc := healthcheck("127.0.0.1:1"); rc == 0 {
		t.Fatal("healthcheck passed with no agent listening")
	}
}

func TestHealthcheckFailsOnANonConformingEE(t *testing.T) {
	srv := testAgent(t, "missing git on PATH")
	addr := strings.TrimPrefix(srv.URL, "http://")
	if rc := healthcheck(addr); rc == 0 {
		t.Fatal("healthcheck passed while the EE did not meet the contract")
	}
}

func TestHealthcheckPassesAgainstAConformingAgent(t *testing.T) {
	// The positive control. Without it the three failure tests above would pass
	// against a healthcheck that always failed.
	srv := testAgent(t, "")
	addr := strings.TrimPrefix(srv.URL, "http://")
	if rc := healthcheck(addr); rc != 0 {
		t.Fatalf("healthcheck failed against a healthy agent: rc=%d", rc)
	}
}

func TestContractNamesEveryBinaryItRequires(t *testing.T) {
	// An empty PATH, so everything is missing and the message must list it all —
	// an operator fixing a custom EE needs the whole set, not the first failure.
	t.Setenv("PATH", t.TempDir())
	reason := checkContract()
	for _, bin := range contractBinaries {
		if !strings.Contains(reason, bin) {
			t.Errorf("the contract failure does not mention %q: %s", bin, reason)
		}
	}
}

func TestContractPassesWhenEverythingIsPresent(t *testing.T) {
	dir := t.TempDir()
	for _, bin := range append([]string{"ansible"}, contractBinaries...) {
		if err := os.WriteFile(dir+"/"+bin, []byte("#!/bin/sh\nexit 0\n"), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("PATH", dir)
	if reason := checkContract(); reason != "" {
		t.Fatalf("a conforming environment was reported as broken: %s", reason)
	}
}

func TestContractCatchesAnsibleThatWillNotRun(t *testing.T) {
	// Every binary on PATH and ansible still unable to start — the shape an EE
	// with an unwritable HOME or no passwd entry for its uid actually has, and
	// the shape a `command -v` check cannot see.
	dir := t.TempDir()
	for _, bin := range contractBinaries {
		_ = os.WriteFile(dir+"/"+bin, []byte("#!/bin/sh\nexit 0\n"), 0o755)
	}
	_ = os.WriteFile(dir+"/ansible",
		[]byte("#!/bin/sh\necho 'Invalid settings supplied for DEFAULT_LOCAL_TMP' >&2\nexit 1\n"), 0o755)
	t.Setenv("PATH", dir)
	reason := checkContract()
	if reason == "" {
		t.Fatal("an EE where ansible cannot run was reported as conforming")
	}
	if !strings.Contains(reason, "will not run as this uid") {
		t.Fatalf("the reason should point at the uid, got: %s", reason)
	}
	if !strings.Contains(reason, "DEFAULT_LOCAL_TMP") {
		t.Fatalf("the reason should carry ansible's own output, got: %s", reason)
	}
}

// get is the read-only twin of post, for /result.
func get(t *testing.T, url, token string) *http.Response {
	t.Helper()
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = resp.Body.Close() })
	return resp
}

func decodeOutcome(t *testing.T, resp *http.Response) outcome {
	t.Helper()
	var o outcome
	if err := json.NewDecoder(resp.Body).Decode(&o); err != nil {
		t.Fatalf("decoding /result: %v", err)
	}
	return o
}

func TestResultRequiresTheToken(t *testing.T) {
	srv := testAgent(t, "")
	if resp := get(t, srv.URL+"/result", ""); resp.StatusCode != http.StatusForbidden {
		t.Fatalf("want 403 without a token, got %d", resp.StatusCode)
	}
}

func TestResultIsNotFoundBeforeAnythingHasRun(t *testing.T) {
	srv := testAgent(t, "")
	if resp := get(t, srv.URL+"/result", "the-token"); resp.StatusCode != http.StatusNotFound {
		t.Fatalf("want 404 before any child, got %d", resp.StatusCode)
	}
}

// The property /result exists for: a caller that reads ONLY the body and this
// endpoint -- never the trailer -- still learns the exit code. That is the
// orchestrator's position exactly, because no Python HTTP client exposes
// trailers (see the `outcome` doc comment). Asserting it here with the trailer
// deliberately ignored is what makes the Go test speak for the Python consumer.
func TestResultCarriesTheExitCodeWithoutReadingTheTrailer(t *testing.T) {
	fakeAnsible(t, "ansible-playbook", `echo doing-work; exit 7`)
	srv := testAgent(t, "")

	resp := post(t, srv.URL+"/exec", "the-token", execRequest{Bin: "ansible-playbook"})
	body, _ := io.ReadAll(resp.Body) // to EOF, which is when the verdict is recorded
	if !strings.Contains(string(body), "doing-work") {
		t.Fatalf("the child's output did not reach the caller: %q", body)
	}

	o := decodeOutcome(t, get(t, srv.URL+"/result", "the-token"))
	if o.RC != 7 {
		t.Fatalf("want rc 7 from /result, got %d", o.RC)
	}
	if o.Bin != "ansible-playbook" {
		t.Fatalf("want the binary named, got %q", o.Bin)
	}
	if o.Err != "" {
		t.Fatalf("a non-zero exit is ansible's verdict, not an agent error, got %q", o.Err)
	}
}

// A refusal before anything starts must reach /result too, or the orchestrator
// cannot tell "the playbook failed" from "the EE has no ansible-playbook" --
// which it otherwise learns only from the X-Terrapod-Error trailer it cannot
// read.
func TestResultReportsARefusalThatNeverStartedAChild(t *testing.T) {
	srv := testAgent(t, "")
	_ = post(t, srv.URL+"/exec", "the-token", execRequest{Bin: "sh"})

	o := decodeOutcome(t, get(t, srv.URL+"/result", "the-token"))
	if o.RC != 126 {
		t.Fatalf("want rc 126 for a non-allowlisted binary, got %d", o.RC)
	}
	if !strings.Contains(o.Err, "not an ansible entry point") {
		t.Fatalf("want the refusal's reason, got %q", o.Err)
	}
}

func TestOnlyGetReadsTheResult(t *testing.T) {
	srv := testAgent(t, "")
	if resp := post(t, srv.URL+"/result", "the-token", nil); resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("want 405 for POST /result, got %d", resp.StatusCode)
	}
}
