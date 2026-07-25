# terrapod-loadtest

A small, reproducible load generator used to characterise Terrapod's horizontal
scaling — the evidence behind [`docs/scalability.md`](../docs/scalability.md)
(issue #1056). It drives the public API through the canonical **go-terrapod**
SDK, so what it measures is what real automation experiences.

It is a test/benchmark tool — not shipped in any image.

## Build

```sh
cd loadtest && go build -o terrapod-loadtest .
```

## Connect

| Env | Default | Meaning |
|---|---|---|
| `TERRAPOD_ADDR` | `https://terrapod.local` | Base URL. Point at a port-forward (`http://localhost:8000`) to measure the API directly, bypassing the BFF. |
| `TERRAPOD_TOKEN` | — | Bearer token. Falls back to `~/.terraform.d/credentials.tfrc.json` for the host (i.e. a `tofu login <host>` token). |
| `TERRAPOD_INSECURE` | unset | `1` to skip TLS verification (local self-signed certs). |

## Subcommands

```sh
# seed N workspaces (agent mode, tiny resources) sharing a name prefix
terrapod-loadtest seed    -n 5000 -c 32 -prefix lt

# hammer the read surface (list/search/get) at fixed concurrency for a duration
terrapod-loadtest bench   -c 32 -d 30s -page-size 20 -prefix lt

# queue plan-only runs against seeded workspaces and watch the queue drain
# (job-scheduling throughput + cross-replica correctness)
terrapod-loadtest flood   -n 200 -c 16 -prefix lt -timeout 15m

# tally latest-run status across a batch (dispatch-correctness check)
terrapod-loadtest verify  -prefix lt

# delete every workspace matching a prefix
terrapod-loadtest cleanup -c 32 -prefix lt
```

`bench` reports p50/p95/p99 latency, throughput, and error rate per op. `flood`
reports run-accept throughput, time-to-dispatch (queued→claimed), the queue-drain
timeline, the terminal breakdown, and the correctness verdict (every run reached
a terminal state exactly once).

## Reproducing the at-scale run locally

Bring the dev stack up in the horizontal-scale profile — API/web/listener HPAs, a
sized embedded Postgres + Redis, and rate limiting disabled so the benchmark
measures capacity rather than the limiter:

```sh
tilt up -- --scale          # layers helm/terrapod/values-scale.yaml
```

Then `seed`, `bench`, and `flood` as above. See
[`docs/scalability.md`](../docs/scalability.md) for the methodology, the rig, the
results, and the honest limits of a single-node measurement.

> **Rate limiting.** The default `authenticated_requests_per_minute` (1000 ≈
> 16.7 req/s) will throttle any real throughput test to the limiter, not the API.
> `values-scale.yaml` disables it; against any other deployment, raise or disable
> the limit for the duration of the test, or you are measuring the limiter.
