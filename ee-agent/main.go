package main

import (
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

// defaultListen is LOOPBACK, deliberately and explicitly. ":7781" would bind
// every interface including the pod IP, which is routable from the whole
// cluster; that one character is the whole isolation property.
const defaultListen = "127.0.0.1:7781"

func main() {
	listenAddr := flag.String("listen", defaultListen, "address to serve on (loopback by default)")
	flag.Usage = func() {
		fmt.Fprintf(os.Stderr, "usage: %s <serve|healthcheck|contract> [flags]\n", os.Args[0])
		flag.PrintDefaults()
	}
	if len(os.Args) < 2 {
		flag.Usage()
		os.Exit(2)
	}
	mode := os.Args[1]
	_ = flag.CommandLine.Parse(os.Args[2:])

	switch mode {
	case "serve":
		os.Exit(serve(*listenAddr))
	case "healthcheck":
		os.Exit(healthcheck(*listenAddr))
	case "contract":
		// Standalone, for the published-image conformance job: it needs the
		// verdict without starting a server.
		if reason := checkContract(); reason != "" {
			fmt.Fprintln(os.Stderr, reason)
			os.Exit(1)
		}
		fmt.Println("execution environment conforms")
		os.Exit(0)
	default:
		fmt.Fprintf(os.Stderr, "unknown mode %q\n", mode)
		flag.Usage()
		os.Exit(2)
	}
}

func serve(addr string) int {
	token := os.Getenv(tokenEnv)
	if token == "" {
		fmt.Fprintf(os.Stderr, "refusing to serve: %s is not set, so nothing could be authorised\n", tokenEnv)
		return 1
	}

	a := &agent{token: token, runner: &runner{}}

	// The contract is checked ONCE, here, rather than on every probe. A failure
	// does not stop the agent listening: it is reported through /healthz so the
	// probe's own output carries the reason, which surfaces in pod events where
	// an operator will actually see it. Refusing to listen would hide the reason
	// in a container log and leave the Job in PodInitializing with no clue.
	if reason := checkContract(); reason != "" {
		a.contractError = reason
		fmt.Fprintln(os.Stderr, "EE CONTRACT NOT MET:", reason)
	}

	ln, err := listen(addr)
	if err != nil {
		fmt.Fprintln(os.Stderr, "listen:", err)
		return 1
	}
	fmt.Printf("terrapod-ee-agent listening on %s\n", addr)

	// SIGTERM forwards ONE SIGINT to the child and then waits, matching the
	// runner's own termination contract: a second signal makes ansible abort
	// ungracefully and it may skip writing what it was part way through.
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGTERM, syscall.SIGINT)
	go func() {
		<-stop
		if a.runner.running() {
			fmt.Fprintln(os.Stderr, "terminating: forwarding one SIGINT to ansible")
			a.runner.cancel()
			deadline := time.Now().Add(childGrace)
			for a.runner.running() && time.Now().Before(deadline) {
				time.Sleep(200 * time.Millisecond)
			}
		}
		os.Exit(0)
	}()

	if err := serveOn(ln, a); err != nil && err != http.ErrServerClosed {
		fmt.Fprintln(os.Stderr, "serve:", err)
		return 1
	}
	return 0
}

// healthcheck is the startupProbe. `exec` of this binary, never httpGet: a
// kubelet httpGet probe connects to the POD IP, which a loopback-only listener
// cannot answer, so an httpGet probe could only pass by binding every interface
// — i.e. by breaking the isolation it is meant to be checking.
//
// It prints the body on failure, because probe output is surfaced in pod events
// and that is where an operator looks first.
func healthcheck(addr string) int {
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Get("http://" + addr + "/healthz")
	if err != nil {
		fmt.Fprintln(os.Stderr, "healthcheck:", err)
		return 1
	}
	defer func() { _ = resp.Body.Close() }()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	if resp.StatusCode != http.StatusOK {
		fmt.Fprintf(os.Stderr, "healthcheck: HTTP %d: %s\n", resp.StatusCode, body)
		return 1
	}
	return 0
}
