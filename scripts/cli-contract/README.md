# The CLI contract suite

Drives the **real `tofu` binary** against a running Terrapod, because nothing
else in CI does (#2341).

## Why this exists

`docs/tfe-cli-surface.md` catalogues 69 endpoints and calls them "a stable
contract" for `terraform` / `tofu`. Until this job, the only automated coverage
of that claim was `tests/api/test_route_contract.py`, which pins that a path
exists and its shape — and cannot see a route that is **served perfectly and
does the wrong thing**. This repository has shipped that twice:

* **#1903** — `POST /runs` was listed, implemented and exercised while
  `save-plan` was read off its body by nobody, so `tofu plan -out` silently
  produced an ordinary run.
* **`seed.py`, 2026-11-02** — `PUT /state-versions/{id}/content` was hardened to
  carry a signed capability in place of the id, and the one caller kept building
  the path from the raw id. It answered 401 for months; nothing noticed, because
  the only thing exercising it was a script nobody ran.

The CLI is also the one consumer that **cannot be fixed forward**. A workspace,
the provider or the UI can be corrected in the next release; a `go-tfe` baked
into the `tofu` binary on someone's laptop cannot.

## OpenTofu only, deliberately

The `terraform` binary is BUSL-1.1, and Terrapod is a TFE replacement, so
running it in *this* project's CI to validate *this* project's TFE-compatible
surface touches the licence's competitive-offering restriction. OpenTofu is
MPL-2.0 with no such restriction and is the engine this project's prose puts
first. Terraform compatibility stays asserted through the shared `go-tfe` both
binaries embed — which is the actual object of the compatibility claim — and the
docs say which engine is *proven* rather than implying both are exercised.

That Terrapod *fetches* the terraform binary at run time for operators is a
different thing: that is the operator's choice about their own deployment.

## TLS is not optional

The CLI forces HTTPS for service discovery. Measured against a plain-HTTP
port-forward, which is how `helm-eval-boot` reaches the stack:

```
Error: Failed to request discovery document:
Get "https://localhost:18080/.well-known/terraform.json":
http: server gave HTTP response to HTTPS client
```

So the job mints its own CA and a leaf for a `.test` hostname, fronts the
port-forward with it, and installs the CA where the Go client will find it. The
chart and `values-eval.yaml` are untouched: that profile is gated, must stay
batteries-included, and this job's TLS is its own concern.

### Terminating TLS is not enough — the terminator has to report the scheme

This is the part that cost a day, so it is written down rather than left to be
rediscovered. The first version of the job used `socat OPENSSL-LISTEN:443 ...
TCP:18080`, which terminates TLS perfectly well and forwards bytes. Discovery,
`init` and `plan` all passed. Then every **state** operation hung: `tofu apply`
for its whole timeout, and `state pull` and `state list` after it, because the
killed apply had left the workspace locked.

The cause is that a state content URL is generated **by the server**, from
`X-Forwarded-Proto` and `X-Forwarded-Host` — deliberately, so a deployment with
an internal ingress hands each caller a hostname it can actually reach
(`tfe_v2._request_base`). A TCP tunnel cannot set a header, so the BFF saw a
plain-HTTP connection from loopback and reported `http`:
`web/src/lib/bff-proxy.ts` preserves an incoming `x-forwarded-proto` and
otherwise falls back to its own connection. The server then correctly handed
`tofu` a `http://` URL, on a port where nothing was listening.

Measured against the real API with crafted headers, which is how this was
settled rather than guessed:

| sent | URL the server returns |
|---|---|
| `X-Forwarded-Proto: http` | `http://terrapod.test/...` |
| `X-Forwarded-Proto: https` | `https://terrapod.test/...` |

The product is right; the harness was lying to it. So the front end is a
**reverse proxy** that sets those headers, which is what a real deployment has
in front of the BFF and why this suite passes against a Traefik-fronted dev
stack. Do not swap it back for a tunnel.

It deliberately does **not** listen on port 80, and the job asserts that with
`curl` rather than claiming it: if the scheme is ever reported wrongly again,
the run has to fail loudly instead of quietly succeeding over `http` and
silently stopping testing what it says it tests.

## `tofu login` is exercised as a protocol, not as a browser

The interactive flow completes in a browser. This drives the **same** OAuth2 +
PKCE exchange non-interactively against the real endpoints, so the protocol is
covered end to end and the browser step is not. Said plainly here rather than
claimed as `tofu login` coverage.

## Running it against a local stack

```sh
python3 scripts/cli-contract/run.py --host terrapod.local \
    --email admin --password admin
```

Against Tilt it needs no CA work — `mkcert` has already made one the system
trusts, which is why this is developed there and only then wired into CI.

`--only <names>` re-shoots a subset and pulls in each one's prerequisites: the
state checks read the working directory and the applied state the lifecycle
builds, so running one alone without them reports a `FileNotFoundError` that
looks like a product failure and is not.
