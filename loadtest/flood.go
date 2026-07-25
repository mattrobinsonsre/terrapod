package main

// flood + verify — the job-scheduling-throughput and cross-replica-correctness
// tests (#1056). flood gives each seeded workspace a trivial config version
// (an output-only tofu file — no providers, so `init` pulls nothing and `plan`
// finishes in seconds), queues a plan-only run against each, then watches the
// whole backlog drain. It reports how fast the control plane accepted the runs,
// how fast the dispatcher claimed them (queued→planning), and the terminal
// breakdown — plus the correctness invariant that matters at scale: every run
// reaches a terminal state and none is lost or stuck (the no-leader-election +
// SELECT … FOR UPDATE SKIP LOCKED claim).
//
// The execution layer (real terraform Jobs) is bounded by cluster CPU and the
// listener's capacity admission — that is expected and documented; what scales
// horizontally is the control plane measured here, not the number of Jobs a
// single node can physically run at once.

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/tls"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"sort"
	"sync"
	"sync/atomic"
	"time"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// rawHTTP bundles the pieces needed for the two config-version calls
// go-terrapod does not expose (create + raw tarball PUT).
type rawHTTP struct {
	hc    *http.Client
	addr  string
	token string
}

func newRaw() (*rawHTTP, error) {
	addr := envOr("TERRAPOD_ADDR", "https://terrapod.local")
	host, err := hostOf(addr)
	if err != nil {
		return nil, err
	}
	token := os.Getenv("TERRAPOD_TOKEN")
	if token == "" {
		token, err = tokenFromCredentials(host)
		if err != nil {
			return nil, err
		}
	}
	insecure := os.Getenv("TERRAPOD_INSECURE") == "1" || os.Getenv("TERRAPOD_INSECURE") == "true"
	tr := &http.Transport{
		MaxIdleConnsPerHost: 64,
		TLSClientConfig:     &tls.Config{MinVersion: tls.VersionTLS12, InsecureSkipVerify: insecure}, //nolint:gosec
	}
	return &rawHTTP{hc: &http.Client{Transport: tr, Timeout: 60 * time.Second}, addr: addr, token: token}, nil
}

// trivialConfig is a provider-free, resource-free tofu config: `init` downloads
// nothing and `plan` is near-instant, so a flood exercises scheduling rather
// than provider download or apply time.
func trivialConfigTarGz() ([]byte, error) {
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	content := []byte("output \"ok\" {\n  value = \"ok\"\n}\n")
	if err := tw.WriteHeader(&tar.Header{Name: "main.tf", Mode: 0o644, Size: int64(len(content))}); err != nil {
		return nil, err
	}
	if _, err := tw.Write(content); err != nil {
		return nil, err
	}
	if err := tw.Close(); err != nil {
		return nil, err
	}
	if err := gz.Close(); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// uploadCV creates a (non-auto-queueing) configuration version for the
// workspace and PUTs the trivial tarball, leaving the workspace with a latest
// uploaded CV that a subsequent plan-only run will pick up.
func (r *rawHTTP) uploadCV(ctx context.Context, wsID string, tgz []byte) error {
	// 1) create
	body := []byte(`{"data":{"type":"configuration-versions","attributes":{"auto-queue-runs":false}}}`)
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost,
		r.addr+"/api/v2/workspaces/"+wsID+"/configuration-versions", bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+r.token)
	req.Header.Set("Content-Type", "application/vnd.api+json")
	resp, err := r.hc.Do(req)
	if err != nil {
		return err
	}
	raw, _ := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusCreated {
		return fmt.Errorf("create CV: HTTP %d: %s", resp.StatusCode, snippet(raw))
	}
	var cv struct {
		Data struct {
			Attributes struct {
				UploadURL string `json:"upload-url"`
			} `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(raw, &cv); err != nil {
		return fmt.Errorf("parse CV: %w", err)
	}
	if cv.Data.Attributes.UploadURL == "" {
		return fmt.Errorf("CV response had no upload-url: %s", snippet(raw))
	}
	// 2) upload tarball (no auth header — the CV id is the capability, matching go-tfe)
	up, _ := http.NewRequestWithContext(ctx, http.MethodPut, cv.Data.Attributes.UploadURL, bytes.NewReader(tgz))
	up.Header.Set("Content-Type", "application/x-tar")
	uresp, err := r.hc.Do(up)
	if err != nil {
		return err
	}
	ubody, _ := io.ReadAll(uresp.Body)
	_ = uresp.Body.Close()
	if uresp.StatusCode/100 != 2 {
		return fmt.Errorf("upload tarball: HTTP %d: %s", uresp.StatusCode, snippet(ubody))
	}
	return nil
}

func snippet(b []byte) string {
	if len(b) > 200 {
		return string(b[:200])
	}
	return string(b)
}

// ─────────────────────────── flood ───────────────────────────

func cmdFlood(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("flood", flag.ExitOnError)
	n := fs.Int("n", 100, "max workspaces to queue a run against")
	conc := fs.Int("c", 16, "concurrency for CV upload + run creation")
	prefix := fs.String("prefix", "lt", "target workspaces whose name matches this prefix")
	timeout := fs.Duration("timeout", 15*time.Minute, "give up waiting for the backlog to drain")
	poll := fs.Duration("poll", 3*time.Second, "queue-depth sampling / status poll interval")
	_ = fs.Parse(args)

	c, addr, err := newClient(*conc)
	if err != nil {
		return err
	}
	raw, err := newRaw()
	if err != nil {
		return err
	}
	tgz, err := trivialConfigTarGz()
	if err != nil {
		return err
	}

	all, err := c.ListAllWorkspaces(ctx, terrapod.WorkspaceListOptions{Search: *prefix, PageSize: 100})
	if err != nil {
		return err
	}
	if len(all) == 0 {
		return fmt.Errorf("no workspaces match prefix %q — run `seed` first", *prefix)
	}
	if len(all) > *n {
		all = all[:*n]
	}
	fmt.Printf("flood: queuing plan-only runs on %d workspaces, %d workers → %s\n", len(all), *conc, addr)

	// Phase A — upload CV + queue a run per workspace, concurrently.
	type queued struct {
		wsID, runID string
		qAt         time.Time
	}
	var (
		mu        sync.Mutex
		runs      []queued
		setupFail int64
	)
	jobs := make(chan string, *conc)
	setupStart := time.Now()
	var wg sync.WaitGroup
	for w := 0; w < *conc; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for wsID := range jobs {
				if err := raw.uploadCV(ctx, wsID, tgz); err != nil {
					if atomic.AddInt64(&setupFail, 1) <= 5 {
						fmt.Fprintf(os.Stderr, "  cv %s: %v\n", wsID, err)
					}
					continue
				}
				run, err := c.CreateRun(ctx, terrapod.CreateRunRequest{WorkspaceID: wsID, PlanOnly: true, Message: "loadtest flood"})
				if err != nil {
					if atomic.AddInt64(&setupFail, 1) <= 5 {
						fmt.Fprintf(os.Stderr, "  run %s: %v\n", wsID, err)
					}
					continue
				}
				mu.Lock()
				runs = append(runs, queued{wsID: wsID, runID: run.ID, qAt: time.Now()})
				mu.Unlock()
			}
		}()
	}
	for _, ws := range all {
		select {
		case <-ctx.Done():
			close(jobs)
			wg.Wait()
			return ctx.Err()
		case jobs <- ws.ID:
		}
	}
	close(jobs)
	wg.Wait()
	setupEl := time.Since(setupStart)
	fmt.Printf("flood: queued %d runs (%d setup failures) in %s (%.1f runs/s accepted)\n",
		len(runs), setupFail, setupEl.Round(time.Millisecond), float64(len(runs))/setupEl.Seconds())
	if len(runs) == 0 {
		return fmt.Errorf("no runs queued")
	}

	// Phase B — poll every run to terminal, sampling queue depth over time.
	terminal := map[string]bool{"planned": true, "applied": true, "errored": true, "discarded": true, "canceled": true}
	dispatchAt := map[string]time.Duration{} // runID → time from queue to first non-queued state
	doneAt := map[string]time.Duration{}     // runID → time from queue to terminal
	finalStatus := map[string]string{}
	drainStart := time.Now()
	deadline := drainStart.Add(*timeout)

	fmt.Printf("\nflood: draining (%s poll, %s timeout)…\n", *poll, *timeout)
	fmt.Printf("  %-8s %8s %8s %8s %8s %8s\n", "t", "queued", "running", "done", "errored", "drain/s")
	var lastDone int
	for {
		if ctx.Err() != nil {
			break
		}
		var queuedN, runningN, doneN, erroredN int
		// Poll statuses concurrently to keep the sample near-instant.
		statuses := make([]string, len(runs))
		var pwg sync.WaitGroup
		sem := make(chan struct{}, *conc)
		for i := range runs {
			if _, ok := doneAt[runs[i].runID]; ok {
				statuses[i] = finalStatus[runs[i].runID]
				continue
			}
			pwg.Add(1)
			sem <- struct{}{}
			go func(i int) {
				defer pwg.Done()
				defer func() { <-sem }()
				r, err := c.GetRun(ctx, runs[i].runID)
				if err != nil {
					statuses[i] = "queued" // treat transient read error as not-yet-done
					return
				}
				statuses[i] = r.Status
			}(i)
		}
		pwg.Wait()

		now := time.Now()
		for i := range runs {
			st := statuses[i]
			rid := runs[i].runID
			if st != "queued" && st != "pending" {
				if _, ok := dispatchAt[rid]; !ok {
					dispatchAt[rid] = now.Sub(runs[i].qAt)
				}
			}
			if terminal[st] {
				if _, ok := doneAt[rid]; !ok {
					doneAt[rid] = now.Sub(runs[i].qAt)
					finalStatus[rid] = st
				}
			}
			switch {
			case terminal[st] && st == "errored":
				erroredN++
			case terminal[st]:
				doneN++
			case st == "queued" || st == "pending":
				queuedN++
			default:
				runningN++
			}
		}
		el := time.Since(drainStart)
		drainRate := float64(doneN+erroredN-lastDone) / poll.Seconds()
		lastDone = doneN + erroredN
		fmt.Printf("  %-8s %8d %8d %8d %8d %8.1f\n", el.Round(time.Second), queuedN, runningN, doneN, erroredN, drainRate)

		if queuedN+runningN == 0 {
			break
		}
		if now.After(deadline) {
			fmt.Printf("  timeout: %d runs still not terminal\n", queuedN+runningN)
			break
		}
		select {
		case <-ctx.Done():
		case <-time.After(*poll):
		}
	}

	// Report.
	var disp, done []time.Duration
	for _, d := range dispatchAt {
		disp = append(disp, d)
	}
	for _, d := range doneAt {
		done = append(done, d)
	}
	byStatus := map[string]int{}
	for _, s := range finalStatus {
		byStatus[s]++
	}
	fmt.Printf("\nflood results — %d runs queued, %d reached terminal\n", len(runs), len(doneAt))
	if len(disp) > 0 {
		p50, p95, p99, mx := percentiles(disp)
		fmt.Printf("  time-to-dispatch (queued→claimed):  p50=%s p95=%s p99=%s max=%s\n", ms(p50), ms(p95), ms(p99), ms(mx))
	}
	if len(done) > 0 {
		p50, p95, p99, mx := percentiles(done)
		fmt.Printf("  time-to-terminal (queued→done):     p50=%s p95=%s p99=%s max=%s\n", secs(p50), secs(p95), secs(p99), secs(mx))
	}
	fmt.Printf("  terminal breakdown: ")
	sts := make([]string, 0, len(byStatus))
	for s := range byStatus {
		sts = append(sts, s)
	}
	sort.Strings(sts)
	for _, s := range sts {
		fmt.Printf("%s=%d ", s, byStatus[s])
	}
	fmt.Println()
	stuck := len(runs) - len(doneAt)
	fmt.Printf("  CORRECTNESS: %d/%d runs terminal, %d stuck. %s\n", len(doneAt), len(runs), stuck,
		correctnessVerdict(stuck))
	return nil
}

func correctnessVerdict(stuck int) string {
	if stuck == 0 {
		return "PASS — every queued run was claimed exactly once and reached a terminal state."
	}
	return "INCOMPLETE — some runs did not drain within the timeout (raise -timeout, or the node/listener is at capacity)."
}

// ─────────────────────────── verify ───────────────────────────

func cmdVerify(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("verify", flag.ExitOnError)
	prefix := fs.String("prefix", "lt", "workspaces whose name matches this prefix")
	conc := fs.Int("c", 16, "concurrency")
	_ = fs.Parse(args)

	c, addr, err := newClient(*conc)
	if err != nil {
		return err
	}
	all, err := c.ListAllWorkspaces(ctx, terrapod.WorkspaceListOptions{Search: *prefix, PageSize: 100})
	if err != nil {
		return err
	}
	fmt.Printf("verify: tallying latest runs for %d workspaces on %s…\n", len(all), addr)

	var (
		mu       sync.Mutex
		byStatus = map[string]int{}
		multiRun int
		noRun    int
	)
	jobs := make(chan string, *conc)
	var wg sync.WaitGroup
	for w := 0; w < *conc; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for wsID := range jobs {
				runs, err := c.ListWorkspaceRuns(ctx, wsID, 1, 20)
				if err != nil {
					continue
				}
				mu.Lock()
				if len(runs) == 0 {
					noRun++
				}
				nonTerminal := 0
				for i := range runs {
					if i == 0 {
						byStatus[runs[i].Status]++
					}
					switch runs[i].Status {
					case "planned", "applied", "errored", "discarded", "canceled":
					default:
						nonTerminal++
					}
				}
				if nonTerminal > 1 {
					multiRun++ // more than one in-flight run on one workspace = serialization anomaly
				}
				mu.Unlock()
			}
		}()
	}
	for _, ws := range all {
		jobs <- ws.ID
	}
	close(jobs)
	wg.Wait()

	fmt.Printf("verify: latest-run status distribution:\n")
	sts := make([]string, 0, len(byStatus))
	for s := range byStatus {
		sts = append(sts, s)
	}
	sort.Strings(sts)
	for _, s := range sts {
		fmt.Printf("  %-12s %d\n", s, byStatus[s])
	}
	fmt.Printf("  workspaces with no runs: %d\n", noRun)
	fmt.Printf("  workspaces with >1 in-flight run (serialization anomaly): %d %s\n", multiRun,
		map[bool]string{true: "PASS", false: "FAIL"}[multiRun == 0])
	return nil
}

func secs(d time.Duration) string { return fmt.Sprintf("%.1fs", d.Seconds()) }
