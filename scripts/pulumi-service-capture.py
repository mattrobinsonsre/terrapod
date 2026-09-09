#!/usr/bin/env python3
"""Capture what the Pulumi CLI asks a state backend for (#1502, part of #1407).

Fifth protocol capture, after Galaxy, plugin-download, GOPROXY and NuGet. The
four before it turned up eleven things the documentation would have had us build
wrong, several invisible to any synthetic client — which is why a protocol gets
captured before it is implemented.

Stands up a request-logging stub, points `pulumi login` at it **under a path
prefix**, and drives a real `login → stack init → stack ls → preview → up →
refresh → export → destroy → stack rm` cycle to completion. The findings are
written up in `docs/pulumi-cli-surface.md`.

    python3 scripts/pulumi-service-capture.py

Requires Docker (for the `pulumi/pulumi-python` image); stdlib only otherwise.

**The findings worth keeping in view**, because each cost a round of iteration:

* Auth is `token <value>`, not `Bearer` — and it **changes mid-run**: calls made
  during an update carry `update-token <lease>` instead.
* The start call (`POST .../update/{id}`) **must return a lease token** or the
  CLI panics with "persisted actions require a token".
* The service is the stack's **secrets provider** — `POST .../encrypt` is called
  during an ordinary `up`.
* An empty stack's deployment is `null`. A hand-made empty one fails the CLI's
  snapshot integrity check.
* Checkpoint and event bodies arrive **gzipped**.
* The CLI appends `/api/...` to the base URL it was given, **path prefix
  included** — so this surface need not be mounted at the root.

**If a run fails with "stack not found" partway through, look for an orphaned
container before believing it.** Interrupting this script leaves its
`pulumi/pulumi-python` container running and still pointed at
`host.containers.internal:8810`, so it talks to the *next* run's stub and mutates
its state underneath it. `docker ps | grep pulumi` and kill the stray one. That
cost an hour of chasing a bug that was not in the code.
"""

from __future__ import annotations

import base64
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8810
#: Deliberately not the host root: proving this surface can live under a prefix
#: is a finding, not an assumption (it is the question #1484 settled for NuGet).
PREFIX = "/api/v1/pulumi"
ORG = "spike"
IMAGE = "pulumi/pulumi-python:latest"
CONTAINER_HOST = "host.containers.internal"

LOG: list[dict] = []
STACKS: dict[str, dict] = {}
DEPLOYMENTS: dict[str, object] = {}
BODY: dict = {}

#: A brand-new stack has a *null* deployment. Anything else — `{}`, or a
#: manifest with an empty resource list — trips the snapshot integrity check.
EMPTY_DEPLOYMENT = None


def _stack_route(method: str, key: str, tail: str):
    if tail == "" and method == "GET":
        if key not in STACKS:
            return 404, {"message": "stack not found"}
        return 200, STACKS[key]
    if tail == "" and method == "DELETE":
        STACKS.pop(key, None)
        return 200, {}

    if tail == "export" and method == "GET":
        return 200, {"version": 3, "deployment": DEPLOYMENTS.get(key, EMPTY_DEPLOYMENT)}
    if tail == "import" and method == "POST":
        DEPLOYMENTS[key] = BODY.get("deployment", EMPTY_DEPLOYMENT)
        return 200, {"updateID": "u-import"}

    # The service is the stack's secrets provider in httpstate mode.
    if tail == "encrypt":
        return 200, {
            "ciphertext": base64.b64encode(
                b"enc:" + BODY.get("plaintext", "").encode()
            ).decode()
        }
    if tail == "decrypt":
        raw = base64.b64decode(BODY.get("ciphertext", "")).decode("utf-8", "replace")
        return 200, {"plaintext": raw.removeprefix("enc:")}
    if tail == "batch-decrypt":
        return 200, {
            "plaintexts": [
                base64.b64decode(c).decode("utf-8", "replace").removeprefix("enc:")
                for c in BODY.get("ciphertexts", [])
            ]
        }

    if (
        tail in ("preview", "update", "refresh", "destroy", "import")
        and method == "POST"
    ):
        return 200, {"updateID": "u1", "requiredPolicies": []}

    seg = tail.split("/")
    if len(seg) >= 2 and seg[0] in (
        "preview",
        "update",
        "refresh",
        "destroy",
        "import",
    ):
        rest = "/".join(seg[2:])
        if rest == "" and method == "POST":
            # Starting the update hands back the LEASE TOKEN. Omit it and the
            # CLI panics: "persisted actions require a token".
            return 200, {"token": "lease-token-1"}
        if rest == "" and method == "GET":
            return 200, {"status": "succeeded"}
        if rest == "renew_lease":
            return 200, {"token": "lease-token-1"}
        if rest in (
            "checkpoint",
            "checkpointverbatim",
            "complete",
            "events",
            "events/batch",
        ):
            return 200, {}
        if rest == "status":
            return 200, {"status": "succeeded"}
    return None


def _routes(path: str, method: str):
    p = path.split("?")[0]
    if PREFIX and p.startswith(PREFIX):
        p = p[len(PREFIX) :] or "/"

    if p == "/api/user":
        return 200, {
            "id": "spike-user",
            "githubLogin": "spike-user",
            "name": "Spike User",
            "email": "spike@example.invalid",
            "organizations": [
                {"githubLogin": ORG, "name": ORG, "avatarUrl": "", "defaultRepo": ""}
            ],
            "identities": ["spike-user"],
            "siteAdmin": False,
        }
    if p == "/api/capabilities":
        return 200, {"capabilities": []}
    if p.startswith("/api/user/organizations/"):
        return 200, {
            "githubLogin": ORG,
            "name": ORG,
            "avatarUrl": "",
            "defaultRepo": "",
        }
    if p == "/api/user/stacks":
        return 200, {"stacks": list(STACKS.values())}

    parts = [x for x in p.split("/") if x]
    if len(parts) >= 3 and parts[0] == "api" and parts[1] == "stacks":
        if method == "POST" and len(parts) == 4:
            name = BODY.get("stackName", "dev")
            STACKS[f"{parts[2]}/{parts[3]}/{name}"] = {
                "orgName": parts[2],
                "projectName": parts[3],
                "stackName": name,
                "currentOperation": None,
                "tags": BODY.get("tags", {}),
                "version": 1,
            }
            return 200, {}
        if len(parts) >= 5:
            key = "/".join(parts[2:5])
            return _stack_route(method, key, "/".join(parts[5:]))
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self) -> None:
        global BODY
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        LOG.append(
            {
                "method": self.command,
                "path": self.path,
                "auth": self.headers.get("Authorization", ""),
                "cenc": self.headers.get("Content-Encoding", ""),
                "len": n,
            }
        )
        try:
            BODY = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Checkpoint and event bodies arrive gzipped, so they are not JSON
            # here — and do not need to be. The path and headers are the capture.
            BODY = {}
        status, payload = _routes(self.path, self.command) or (
            404,
            {"message": "not found"},
        )
        blob = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _reply

    def log_message(self, *_a: object) -> None:
        return


SCRIPT = r"""
set -u
cd /work/proj
export PULUMI_HOME=/work/home PULUMI_SKIP_UPDATE_CHECK=true
export PULUMI_ACCESS_TOKEN=${PULUMI_TOKEN:-pul-spiketoken}
python -m venv /work/venv >/dev/null 2>&1
export PATH=/work/venv/bin:$PATH
/work/venv/bin/pip install -q pulumi pulumi-random >/dev/null 2>&1
for c in "login $BACKEND" "whoami" "stack init ${PULUMI_ORG:-spike}/proj/dev" "stack ls" \
         "preview" "up --yes" "refresh --yes"; do
  echo "=== pulumi $c ==="
  pulumi $c --non-interactive 2>&1 | tail -3
done

# export/import need a file, so they sit outside the loop. `stack import` is
# asynchronous: it returns an updateID and the CLI then polls
# GET .../update/{id} until it reports a terminal status.
echo "=== pulumi stack export ==="
pulumi stack export > /tmp/deployment.json 2>/dev/null && head -c 60 /tmp/deployment.json && echo
echo "=== pulumi stack import ==="
pulumi stack import --file /tmp/deployment.json 2>&1 | tail -2

for c in "destroy --yes" "stack rm dev --yes"; do
  echo "=== pulumi $c ==="
  pulumi $c --non-interactive 2>&1 | tail -3
done
"""

PROGRAM = (
    "import pulumi\n"
    "import pulumi_random as random\n"
    's = random.RandomString("s", length=8, special=False)\n'
    'pulumi.export("s", s.result)\n'
)


def _normalise(path: str) -> str:
    p = path.split("?")[0]
    if PREFIX and p.startswith(PREFIX):
        p = p[len(PREFIX) :]
    p = re.sub(r"/api/stacks/[^/]+/[^/]+/[^/]+", "/api/stacks/{stack}", p)
    p = re.sub(r"/(update|preview|refresh|destroy|import)/[^/]+", r"/\1/{updateID}", p)
    return re.sub(r"/api/user/organizations/[^/]+", "/api/user/organizations/{org}", p)


def main() -> int:
    if shutil.which("docker") is None:
        print("docker is required (for the pulumi image)", file=sys.stderr)
        return 2

    # `--backend URL --token TOKEN` drives the same cycle against a REAL
    # Terrapod instead of the logging stub. That is the difference between
    # knowing what the CLI asks for and knowing that what we built answers it —
    # the stub agrees with whatever you write, so a capture against it can only
    # ever confirm the capture.
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="", help="a live Terrapod base URL, e.g. http://host:3000")
    ap.add_argument("--token", default="", help="a Terrapod API token for --backend")
    args = ap.parse_args()

    srv = None
    if args.backend:
        backend = args.backend.rstrip("/") + PREFIX
        token = args.token
        print(f"driving a real backend: {backend}")
    else:
        srv = HTTPServer(("0.0.0.0", PORT), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(0.3)
        backend = f"http://{CONTAINER_HOST}:{PORT}{PREFIX}"
        token = "pul-spiketoken"

    work = pathlib.Path(tempfile.mkdtemp(prefix="pulumi-svc-capture-"))
    proj = work / "proj"
    proj.mkdir()
    (proj / "Pulumi.yaml").write_text(
        "name: proj\nruntime: python\ndescription: capture\n"
    )
    (proj / "__main__.py").write_text(PROGRAM)
    (work / "run.sh").write_text(SCRIPT)

    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-e",
            f"BACKEND={backend}",
            "-e",
            f"PULUMI_TOKEN={token}",
            "-e",
            f"PULUMI_ORG={'default' if args.backend else 'spike'}",
            "-v",
            f"{work}:/work",
            "-w",
            "/work",
            IMAGE,
            "bash",
            "/work/run.sh",
        ],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    if srv is not None:
        srv.shutdown()
    print(proc.stdout)

    seen: dict[tuple[str, str], dict] = {}
    for e in LOG:
        d = seen.setdefault(
            (e["method"], _normalise(e["path"])), {"n": 0, "auth": set(), "enc": set()}
        )
        d["n"] += 1
        if e["auth"]:
            d["auth"].add(e["auth"].split()[0])
        if e["cenc"]:
            d["enc"].add(e["cenc"])

    print(f"=== {len(LOG)} requests, {len(seen)} distinct endpoints ===")
    for (method, path), d in seen.items():
        auth = "/".join(sorted(d["auth"])) or "-"
        enc = ",".join(sorted(d["enc"])) or "-"
        print(f"  {method:6s} {path:56s} x{d['n']:<3} auth={auth:13s} enc={enc}")

    shutil.rmtree(work, ignore_errors=True)
    print(
        f"\nEvery path sits under {PREFIX}: this surface need not be at the host root."
    )
    print("Compare against docs/pulumi-cli-surface.md; update it if the CLI has moved.")
    return 0 if LOG else 1


if __name__ == "__main__":
    raise SystemExit(main())
