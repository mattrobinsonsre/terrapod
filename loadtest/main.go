// Command terrapod-loadtest is a small, reproducible load generator used to
// characterise Terrapod's horizontal-scaling behaviour (issue #1056). It drives
// the public API through the canonical go-terrapod SDK — the same client the
// provider, migrate, and publish tools use — so what it measures is what real
// automation experiences, not a synthetic path.
//
// It is a test/benchmark tool, NOT shipped in any image. Subcommands:
//
//	seed     create N workspaces concurrently (agent mode, tiny resources) so a
//	         large estate can be measured. Names share a prefix+batch so bench
//	         and cleanup can target them.
//	bench    hammer the read surface (list/search/get) at a fixed concurrency
//	         for a duration; report p50/p95/p99 latency, throughput, error rate.
//	flood    queue plan-only runs against seeded workspaces and watch the queue
//	         drain — the job-scheduling-throughput + cross-replica-correctness
//	         test (no lost/duplicated runs).
//	verify   tally runs by status for a batch (dispatch-correctness check).
//	cleanup  delete every workspace matching a prefix.
//
// Connection: TERRAPOD_ADDR (default https://terrapod.local), TERRAPOD_TOKEN
// (falls back to ~/.terraform.d/credentials.tfrc.json for the host),
// TERRAPOD_INSECURE=1 to skip TLS verification (local self-signed certs).
package main

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	var err error
	switch os.Args[1] {
	case "seed":
		err = cmdSeed(ctx, os.Args[2:])
	case "bench":
		err = cmdBench(ctx, os.Args[2:])
	case "flood":
		err = cmdFlood(ctx, os.Args[2:])
	case "verify":
		err = cmdVerify(ctx, os.Args[2:])
	case "cleanup":
		err = cmdCleanup(ctx, os.Args[2:])
	case "-h", "--help", "help":
		usage()
		return
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand %q\n\n", os.Args[1])
		usage()
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `terrapod-loadtest — reproducible Terrapod scaling load generator (#1056)

usage:
  terrapod-loadtest seed    -n 5000 -c 32 -prefix lt
  terrapod-loadtest bench   -c 32 -d 30s -page-size 20 -prefix lt
  terrapod-loadtest flood   -n 200 -c 16 -prefix lt -timeout 10m
  terrapod-loadtest verify  -prefix lt
  terrapod-loadtest cleanup -c 32 -prefix lt

env:
  TERRAPOD_ADDR      base URL (default https://terrapod.local)
  TERRAPOD_TOKEN     bearer token (else read from ~/.terraform.d/credentials.tfrc.json)
  TERRAPOD_INSECURE  set to 1 to skip TLS verification (local self-signed certs)
`)
}

// ─────────────────────────── client ───────────────────────────

// newClient builds a go-terrapod client with a connection pool sized for the
// requested concurrency, so the load generator is not itself the bottleneck.
func newClient(concurrency int) (*terrapod.Client, string, error) {
	addr := envOr("TERRAPOD_ADDR", "https://terrapod.local")
	host, err := hostOf(addr)
	if err != nil {
		return nil, "", err
	}
	token := os.Getenv("TERRAPOD_TOKEN")
	if token == "" {
		token, err = tokenFromCredentials(host)
		if err != nil {
			return nil, "", fmt.Errorf("no TERRAPOD_TOKEN and %w", err)
		}
	}
	insecure := os.Getenv("TERRAPOD_INSECURE") == "1" || os.Getenv("TERRAPOD_INSECURE") == "true"

	// A pooled transport: enough idle conns per host to keep every worker
	// busy without reconnecting. TLS 1.2 min because the local BFF/ingress
	// may not negotiate 1.3 for every path.
	if concurrency < 1 {
		concurrency = 1
	}
	tr := &http.Transport{
		MaxIdleConns:        concurrency * 2,
		MaxIdleConnsPerHost: concurrency * 2,
		IdleConnTimeout:     90 * time.Second,
		TLSClientConfig:     &tls.Config{MinVersion: tls.VersionTLS12, InsecureSkipVerify: insecure}, //nolint:gosec
	}
	hc := &http.Client{Transport: tr, Timeout: 60 * time.Second}

	c, err := terrapod.NewClient(terrapod.Options{
		BaseURL:    addr,
		Token:      token,
		HTTPClient: hc,
		UserAgent:  "terrapod-loadtest",
		MaxRetries: 1, // measure the server, not the SDK's retry masking
	})
	if err != nil {
		return nil, "", err
	}
	return c, addr, nil
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func hostOf(addr string) (string, error) {
	u, err := url.Parse(addr)
	if err != nil {
		return "", err
	}
	if u.Host == "" {
		return addr, nil
	}
	return u.Host, nil
}

// tokenFromCredentials reads the terraform CLI credentials file the same way
// the CLI does, so a `tofu login <host>` token just works.
func tokenFromCredentials(host string) (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	path := filepath.Join(home, ".terraform.d", "credentials.tfrc.json")
	raw, err := os.ReadFile(path) //nolint:gosec // well-known path
	if err != nil {
		return "", fmt.Errorf("read %s: %w", path, err)
	}
	var doc struct {
		Credentials map[string]struct {
			Token string `json:"token"`
		} `json:"credentials"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		return "", fmt.Errorf("parse %s: %w", path, err)
	}
	if cred, ok := doc.Credentials[host]; ok && cred.Token != "" {
		return cred.Token, nil
	}
	return "", fmt.Errorf("no token for host %q in credentials.tfrc.json", host)
}

// ─────────────────────────── seed ───────────────────────────

func cmdSeed(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("seed", flag.ExitOnError)
	n := fs.Int("n", 1000, "number of workspaces to create")
	conc := fs.Int("c", 32, "concurrent creators")
	prefix := fs.String("prefix", "lt", "workspace name prefix")
	cpu := fs.String("cpu", "250m", "per-workspace resource-cpu request (low so jobs pack)")
	mem := fs.String("mem", "256Mi", "per-workspace resource-memory request")
	pool := fs.String("pool", "", "agent pool id (apool-...) to assign — required for runs to dispatch in `flood`")
	_ = fs.Parse(args)

	c, addr, err := newClient(*conc)
	if err != nil {
		return err
	}
	batch := time.Now().UTC().Format("20060102-150405")
	fmt.Printf("seed: %d workspaces, %d workers, prefix=%s batch=%s → %s\n", *n, *conc, *prefix, batch, addr)

	var created, failed int64
	agent := "agent"
	jobs := make(chan int, *conc)
	start := time.Now()
	var wg sync.WaitGroup
	for w := 0; w < *conc; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := range jobs {
				name := fmt.Sprintf("%s-%s-%06d", *prefix, batch, i)
				_, err := c.CreateWorkspace(ctx, terrapod.CreateWorkspaceRequest{
					Name:           name,
					ExecutionMode:  agent,
					ResourceCPU:    *cpu,
					ResourceMemory: *mem,
					AgentPoolID:    *pool,
					Labels:         map[string]string{"loadtest": "true", "batch": batch},
				})
				if err != nil {
					if atomic.AddInt64(&failed, 1) <= 5 {
						fmt.Fprintf(os.Stderr, "  create %s: %v\n", name, err)
					}
					continue
				}
				cnt := atomic.AddInt64(&created, 1)
				if cnt%500 == 0 {
					fmt.Printf("  created %d/%d (%.0f/s)\n", cnt, *n, float64(cnt)/time.Since(start).Seconds())
				}
			}
		}()
	}
	for i := 0; i < *n; i++ {
		select {
		case <-ctx.Done():
			close(jobs)
			wg.Wait()
			return ctx.Err()
		case jobs <- i:
		}
	}
	close(jobs)
	wg.Wait()
	el := time.Since(start)
	fmt.Printf("seed done: created=%d failed=%d in %s (%.1f/s)\n",
		created, failed, el.Round(time.Millisecond), float64(created)/el.Seconds())
	return nil
}

// ─────────────────────────── bench ───────────────────────────

type sample struct {
	op  string
	dur time.Duration
	err bool
}

func cmdBench(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("bench", flag.ExitOnError)
	conc := fs.Int("c", 32, "concurrent clients")
	dur := fs.Duration("d", 30*time.Second, "test duration")
	pageSize := fs.Int("page-size", 20, "page size for list ops")
	prefix := fs.String("prefix", "lt", "prefix to draw search terms + sample ids from")
	_ = fs.Parse(args)

	c, addr, err := newClient(*conc)
	if err != nil {
		return err
	}

	// Preload a pool of ids/names to exercise get + search realistically.
	fmt.Printf("bench: preloading sample ids (prefix=%s)…\n", *prefix)
	all, err := c.ListAllWorkspaces(ctx, terrapod.WorkspaceListOptions{Search: *prefix, PageSize: 100})
	if err != nil {
		return fmt.Errorf("preload: %w", err)
	}
	if len(all) == 0 {
		return fmt.Errorf("no workspaces match prefix %q — run `seed` first", *prefix)
	}
	ids := make([]string, len(all))
	for i := range all {
		ids[i] = all[i].ID
	}
	total := len(all)
	fmt.Printf("bench: %d clients, %s, %d workspaces in estate → %s\n", *conc, *dur, total, addr)

	deadline := time.Now().Add(*dur)
	samples := make([][]sample, *conc)
	var wg sync.WaitGroup
	for w := 0; w < *conc; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			local := make([]sample, 0, 4096)
			rng := uint64(w*2654435761 + 1) // cheap deterministic per-worker PRNG
			next := func(mod int) int { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return int(rng % uint64(mod)) }
			for time.Now().Before(deadline) {
				if ctx.Err() != nil {
					break
				}
				var (
					op  string
					t0  = time.Now()
					err error
				)
				switch next(10) {
				case 0, 1, 2, 3, 4, 5: // 60% list (paged) — the headline "large estate" op
					op = "list"
					page := next(maxi(1, total/(*pageSize))) + 1
					_, err = c.ListWorkspaces(ctx, terrapod.WorkspaceListOptions{PageNumber: page, PageSize: *pageSize})
				case 6, 7: // 20% search
					op = "search"
					_, err = c.ListWorkspaces(ctx, terrapod.WorkspaceListOptions{Search: *prefix, PageSize: *pageSize})
				default: // 20% get-by-id
					op = "get"
					_, err = c.GetWorkspace(ctx, ids[next(len(ids))])
				}
				local = append(local, sample{op: op, dur: time.Since(t0), err: err != nil})
			}
			samples[w] = local
		}(w)
	}
	wg.Wait()

	merged := map[string][]time.Duration{}
	var errs, count int
	for _, ls := range samples {
		for _, s := range ls {
			count++
			if s.err {
				errs++
			}
			merged[s.op] = append(merged[s.op], s.dur)
			merged["ALL"] = append(merged["ALL"], s.dur)
		}
	}
	fmt.Printf("\nbench results — %d requests, %d errors (%.2f%%), %.0f req/s\n",
		count, errs, pct(errs, count), float64(count)/dur.Seconds())
	fmt.Printf("  %-8s %8s %8s %8s %8s %8s\n", "op", "n", "p50", "p95", "p99", "max")
	for _, op := range []string{"list", "search", "get", "ALL"} {
		ds := merged[op]
		if len(ds) == 0 {
			continue
		}
		p50, p95, p99, mx := percentiles(ds)
		fmt.Printf("  %-8s %8d %8s %8s %8s %8s\n", op, len(ds), ms(p50), ms(p95), ms(p99), ms(mx))
	}
	return nil
}

// ─────────────────────────── cleanup ───────────────────────────

func cmdCleanup(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("cleanup", flag.ExitOnError)
	conc := fs.Int("c", 32, "concurrent deleters")
	prefix := fs.String("prefix", "lt", "delete workspaces whose name matches this prefix")
	_ = fs.Parse(args)

	c, addr, err := newClient(*conc)
	if err != nil {
		return err
	}
	fmt.Printf("cleanup: listing workspaces matching %q on %s…\n", *prefix, addr)
	all, err := c.ListAllWorkspaces(ctx, terrapod.WorkspaceListOptions{Search: *prefix, PageSize: 100})
	if err != nil {
		return err
	}
	fmt.Printf("cleanup: deleting %d workspaces with %d workers…\n", len(all), *conc)

	var deleted, failed int64
	jobs := make(chan string, *conc)
	start := time.Now()
	var wg sync.WaitGroup
	for w := 0; w < *conc; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for id := range jobs {
				if err := c.DeleteWorkspace(ctx, id); err != nil {
					if atomic.AddInt64(&failed, 1) <= 5 {
						fmt.Fprintf(os.Stderr, "  delete %s: %v\n", id, err)
					}
					continue
				}
				if n := atomic.AddInt64(&deleted, 1); n%500 == 0 {
					fmt.Printf("  deleted %d (%.0f/s)\n", n, float64(n)/time.Since(start).Seconds())
				}
			}
		}()
	}
	for _, w := range all {
		select {
		case <-ctx.Done():
			close(jobs)
			wg.Wait()
			return ctx.Err()
		case jobs <- w.ID:
		}
	}
	close(jobs)
	wg.Wait()
	fmt.Printf("cleanup done: deleted=%d failed=%d in %s\n", deleted, failed, time.Since(start).Round(time.Millisecond))
	return nil
}

// ─────────────────────────── flood + verify (job scheduling) ───────────────────────────
// Implemented in flood.go.

// ─────────────────────────── stats helpers ───────────────────────────

func percentiles(ds []time.Duration) (p50, p95, p99, max time.Duration) {
	if len(ds) == 0 {
		return
	}
	sort.Slice(ds, func(i, j int) bool { return ds[i] < ds[j] })
	q := func(p float64) time.Duration {
		idx := int(p * float64(len(ds)-1))
		return ds[idx]
	}
	return q(0.50), q(0.95), q(0.99), ds[len(ds)-1]
}

func ms(d time.Duration) string { return fmt.Sprintf("%.1fms", float64(d.Microseconds())/1000.0) }
func pct(a, b int) float64 {
	if b == 0 {
		return 0
	}
	return 100 * float64(a) / float64(b)
}
func maxi(a, b int) int {
	if a > b {
		return a
	}
	return b
}

var _ = errors.New // reserved for future typed-error handling in flood.go
