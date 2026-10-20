package main

import (
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"time"
)

// tokenEnv carries the shared secret into the agent. The orchestrator holds the
// same value; it is scrubbed from every child the agent starts.
//
// credential. The value lives only in the per-run Secret the listener writes.
//
//nolint:gosec // G101: this is the NAME of an environment variable, not a
const tokenEnv = "TP_EE_TOKEN"

// execRequest is one ansible invocation. `Bin` is checked against the allowlist
// rather than being a path, so a caller cannot name an arbitrary executable.
type execRequest struct {
	Bin  string   `json:"bin"`
	Args []string `json:"args"`
	Env  []string `json:"env"`
	Dir  string   `json:"dir"`
}

type agent struct {
	token  string
	runner *runner
	// contractError is non-empty when the surrounding EE does not meet the
	// contract. Reported through /healthz rather than by refusing to listen, so
	// the reason reaches pod events instead of dying in a container log.
	contractError string
}

// authorised compares the bearer token in constant time. `==` on a secret is the
// kind of thing that is fine until it is not, and the PKCE verifier comparison
// elsewhere in this project uses the same helper for the same reason.
func (a *agent) authorised(r *http.Request) bool {
	want := "Bearer " + a.token
	got := r.Header.Get("Authorization")
	return subtle.ConstantTimeCompare([]byte(got), []byte(want)) == 1
}

func (a *agent) handler() http.Handler {
	mux := http.NewServeMux()

	// Deliberately unauthenticated and deliberately inert. It reveals only that
	// the agent is listening, and the exec probe that reads it has no way to
	// carry a credential.
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		if a.contractError != "" {
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte(a.contractError + "\n"))
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	})

	mux.HandleFunc("/exec", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST only", http.StatusMethodNotAllowed)
			return
		}
		if !a.authorised(r) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		var req execRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "malformed request: "+err.Error(), http.StatusBadRequest)
			return
		}

		// Streamed as it arrives, with the trailer carrying the exit code. The
		// orchestrator tees the body into the run log, so buffering here would
		// make a configure show nothing until it finished.
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.Header().Set("Trailer", "X-Terrapod-RC, X-Terrapod-Error")
		w.WriteHeader(http.StatusOK)
		flusher, _ := w.(http.Flusher)
		sink := &flushWriter{w: w, f: flusher}

		env := scrubbed(os.Environ(), req.Env, tokenEnv)
		rc, err := a.runner.start(req.Bin, req.Args, env, req.Dir, sink)

		w.Header().Set("X-Terrapod-RC", fmt.Sprintf("%d", rc))
		if err != nil {
			w.Header().Set("X-Terrapod-Error", err.Error())
		}
	})

	mux.HandleFunc("/result", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "GET only", http.StatusMethodNotAllowed)
			return
		}
		if !a.authorised(r) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		// The exit code again, out of band, because the orchestrator is Python
		// and no Python client exposes HTTP trailers (see `outcome`). Read after
		// the /exec body reaches EOF: `start` records under the mutex before it
		// returns, so by then the verdict is already there.
		o := a.runner.result()
		if o == nil {
			http.Error(w, "no child has completed in this agent", http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(o)
	})

	mux.HandleFunc("/cancel", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST only", http.StatusMethodNotAllowed)
			return
		}
		if !a.authorised(r) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		// An endpoint rather than a pod kill, because the agent is the PARENT of
		// ansible and can signal it directly. The body says whether there was
		// anything to cancel, so the caller does not report stopping a run that
		// had already finished.
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusAccepted)
		_ = json.NewEncoder(w).Encode(map[string]bool{"signalled": a.runner.cancel()})
	})

	return mux
}

// flushWriter pushes each chunk out rather than letting it sit in a buffer,
// which is what makes the stream live for the orchestrator reading it.
type flushWriter struct {
	w http.ResponseWriter
	f http.Flusher
}

func (fw *flushWriter) Write(p []byte) (int, error) {
	n, err := fw.w.Write(p)
	if fw.f != nil {
		fw.f.Flush()
	}
	return n, err
}

// listen binds the agent. The address is explicit and defaults to loopback:
// ":7781" would bind every interface including the pod IP, which is routable
// from the whole cluster, and that single character is the difference between
// the orchestrator reaching the agent and anything in the cluster reaching it.
func listen(addr string) (net.Listener, error) {
	return net.Listen("tcp", addr)
}

func serveOn(ln net.Listener, a *agent) error {
	srv := &http.Server{
		Handler: a.handler(),
		// A header read timeout only. There is deliberately no write or idle
		// timeout: a configure legitimately streams for as long as the playbook
		// takes, and a timeout here would sever a run that was working.
		ReadHeaderTimeout: 10 * time.Second,
	}
	return srv.Serve(ln)
}
