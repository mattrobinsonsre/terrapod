#!/usr/bin/env python3
"""Capture the Pulumi CLI's secondary commands, against a stub and a real Terrapod (#1571).

`pulumi-service-capture.py` recorded the main lifecycle: login, stack init,
preview, up, refresh, export, import, destroy and stack rm. This covers the rest:

    stack history, stack tag set/ls/rm, stack rename, pulumi cancel,
    stack change-secrets-provider (passphrase, back to the service, awskms),
    state protect/unprotect/delete/edit, and lease renewal on a long update.

Two passes, because they answer different questions:

* **stub** (default): a permissive recording stub. Any route it does not know
  answers 200 `{}`, so the CLI carries on and every request it would make is
  logged. The start-update response carries a short `tokenExpiration`, which is
  what makes the CLI renew its lease inside a short run. This is *what the CLI
  sends*.
* **live** (`--backend https://terrapod.local --token …`): a logging reverse
  proxy in front of a real deployment. The traffic still goes through the
  deployment's own front door (the BFF), and every request is recorded with the
  status Terrapod actually returned. This is *what Terrapod answers*. The
  workspace `proj::capture` is created through the native API first — the CLI
  cannot create one (#1535) — and deleted afterwards.

    python3 scripts/pulumi-secondary-capture.py
    python3 scripts/pulumi-secondary-capture.py --backend https://terrapod.local --token "$TOKEN"

Requires Docker (the `pulumi/pulumi-python` image); stdlib only otherwise. For a
backend with a private CA (mkcert on a dev stack), pass `--cafile`, or it is
looked up with `mkcert -CAROOT`.

Findings are written up in `docs/pulumi-cli-surface.md`.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import http.client
import json
import pathlib
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8812
PREFIX = "/api/v1/pulumi"
IMAGE = "pulumi/pulumi-python:latest"
CONTAINER_HOST = "host.containers.internal"
PROJECT = "proj"

#: (step, method, path, status) in arrival order. A marker request
#: (`GET /__step/<name>`) opens each step, so every call is attributed to the
#: command that made it.
LOG: list[tuple[str, str, str, int]] = []
#: Request bodies of the secondary routes, whose shapes are the point of the capture.
BODIES: list[tuple[str, str, str, str]] = []
STEP = ["setup"]
LOCK = threading.Lock()

# ── the stub ──────────────────────────────────────────────────────────────────

STACKS: dict[str, dict] = {}
#: The update each stack has in flight, reported as `activeUpdate` so `pulumi cancel` has something to cancel.
ACTIVE: dict[str, str] = {}
DEPLOYMENTS: dict[str, object] = {}


def _stub(method: str, path: str, body: dict) -> tuple[int, object]:
    p = path.split("?")[0]
    p = p[len(PREFIX) :] if p.startswith(PREFIX) else p
    if p == "/api/user":
        return 200, {
            "githubLogin": "capture",
            "name": "Capture",
            "organizations": [{"githubLogin": "spike", "name": "spike"}],
        }
    if p == "/api/capabilities":
        return 200, {"capabilities": []}
    if p.startswith("/api/user/organizations/"):
        return 200, {"githubLogin": "spike", "name": "spike"}
    if p == "/api/user/stacks":
        return 200, {"stacks": list(STACKS.values())}
    parts = [x for x in p.split("/") if x]
    if len(parts) == 4 and parts[:2] == ["api", "stacks"] and method == "POST":
        name = body.get("stackName", "capture")
        STACKS[f"{parts[2]}/{parts[3]}/{name}"] = {
            "orgName": parts[2],
            "projectName": parts[3],
            "stackName": name,
            "tags": body.get("tags", {}),
            "version": 1,
        }
        return 200, {}
    if len(parts) < 5 or parts[:2] != ["api", "stacks"]:
        return 404, {"message": "not found"}
    key, tail = "/".join(parts[2:5]), "/".join(parts[5:])
    if tail == "" and method == "GET":
        if key not in STACKS:
            return 404, {"message": "stack not found"}
        return 200, {**STACKS[key], "activeUpdate": ACTIVE.get(key, "")}
    if tail == "" and method == "DELETE":
        STACKS.pop(key, None)
        return 200, {}
    if tail == "rename" and method == "POST":
        new = body.get("newName", "")
        if new and key in STACKS:
            org, proj, _ = key.split("/")
            s = STACKS.pop(key)
            s["stackName"] = new
            STACKS[f"{org}/{proj}/{new}"] = s
            DEPLOYMENTS[f"{org}/{proj}/{new}"] = DEPLOYMENTS.pop(key, None)
        return 200, {}
    if tail == "export":
        return 200, {"version": 3, "deployment": DEPLOYMENTS.get(key)}
    if tail == "import":
        DEPLOYMENTS[key] = body.get("deployment")
        return 200, {"updateID": "u-import"}
    # Plaintexts arrive base64 and may be raw bytes, not text: the CLI encrypts
    # binary. Wrap the decoded bytes, and hand them back base64.
    if tail == "encrypt":
        raw = base64.b64decode(body.get("plaintext", ""))
        return 200, {"ciphertext": base64.b64encode(b"enc:" + raw).decode()}
    if tail == "decrypt":
        raw = base64.b64decode(body.get("ciphertext", "")).removeprefix(b"enc:")
        return 200, {"plaintext": base64.b64encode(raw).decode()}
    if tail == "batch-decrypt":
        # A map from ciphertext to base64 plaintext, not a list: the CLI decodes
        # `plaintexts` as map[string][]byte and fails the whole deployment read
        # on an array.
        return 200, {
            "plaintexts": {
                c: base64.b64encode(base64.b64decode(c).removeprefix(b"enc:")).decode()
                for c in body.get("ciphertexts", [])
            }
        }
    if tail in ("preview", "update", "refresh", "destroy") and method == "POST":
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
            ACTIVE[key] = seg[1]
            # A short expiration is what makes the CLI renew inside a short run.
            return 200, {"token": "lease-1", "tokenExpiration": int(time.time()) + 20}
        if rest == "complete":
            ACTIVE.pop(key, None)
            return 200, {}
        if rest == "renew_lease":
            return 200, {"token": "lease-1", "tokenExpiration": int(time.time()) + 20}
        if rest == "" and method == "GET":
            return 200, {"status": "succeeded"}
        if (
            rest in ("checkpoint", "checkpointverbatim")
            and isinstance(body, dict)
            and "deployment" in body
        ):
            DEPLOYMENTS[key] = body["deployment"]
        return 200, {}
    if tail.startswith("updates"):
        return 200, {"updates": []}
    # Unknown to the stub: answer permissively so the CLI shows what comes next.
    return 200, {}


# ── the handler: stub, or a logging proxy in front of a real deployment ───────


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream: urllib.parse.SplitResult | None = None
    ssl_ctx: ssl.SSLContext | None = None

    def _serve(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if self.path.startswith("/__step/"):
            with LOCK:
                STEP[0] = self.path.removeprefix("/__step/")
            self._send(200, b"{}", "application/json")
            return
        try:
            plain = (
                gzip.decompress(raw)
                if self.headers.get("Content-Encoding") == "gzip"
                else raw
            )
            body = json.loads(plain) if plain else {}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        tail = self.path.split("?")[0].rsplit("/", 1)[-1]
        if (
            tail in ("tags", "rename", "cancel", "renew_lease", "import", "updates")
            or "cancel" in self.path
        ):
            with LOCK:
                BODIES.append(
                    (STEP[0], self.command, self.path, json.dumps(body)[:300])
                )
        if self.upstream is None:
            status, payload = _stub(self.command, self.path, body)
            blob, ctype = json.dumps(payload).encode(), "application/json"
        else:
            status, blob, ctype = self._forward(raw)
        with LOCK:
            LOG.append((STEP[0], self.command, self.path.split("?")[0], status))
        self._send(status, blob, ctype)

    def _forward(self, raw: bytes) -> tuple[int, bytes, str]:
        up = self.upstream
        assert up is not None
        conn = (
            http.client.HTTPSConnection(
                up.hostname, up.port or 443, context=self.ssl_ctx, timeout=120
            )
            if up.scheme == "https"
            else http.client.HTTPConnection(up.hostname, up.port or 80, timeout=120)
        )
        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower()
            in (
                "authorization",
                "content-type",
                "content-encoding",
                "accept",
                "user-agent",
                "accept-encoding",
            )
        }
        conn.request(self.command, self.path, body=raw or None, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        ctype = resp.getheader("Content-Type", "application/json")
        enc = resp.getheader("Content-Encoding")
        conn.close()
        if enc:
            # Pass the encoding through rather than decode: the CLI asked for it.
            self._enc = enc
        return resp.status, data, ctype

    def _send(self, status: int, blob: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if getattr(self, "_enc", None):
            self.send_header("Content-Encoding", self._enc)
            self._enc = None
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _serve

    def log_message(self, *_a: object) -> None:
        return


# ── the CLI side ──────────────────────────────────────────────────────────────

PROGRAM = """import os, time
import pulumi
import pulumi_random as random

time.sleep(int(os.environ.get("CAPTURE_SLEEP", "0")))
s = random.RandomString("s", length=8, special=False)
d = random.RandomPet("d")
pulumi.export("s", s.result)
"""

SCRIPT = r"""
set -u
cd /work/proj
export PULUMI_HOME=/work/home PULUMI_SKIP_UPDATE_CHECK=true PULUMI_ACCESS_TOKEN="$PULUMI_TOKEN"
export PULUMI_CONFIG_PASSPHRASE=capture
python -m venv /work/venv >/dev/null 2>&1
export PATH=/work/venv/bin:$PATH
/work/venv/bin/pip install -q pulumi pulumi-random >/dev/null 2>&1
mark() { python -c "import urllib.request;urllib.request.urlopen('$MARK/__step/$1')" >/dev/null 2>&1; echo "=== $1 ==="; }
run() { "$@" --non-interactive > /work/out.txt 2>&1; rc=$?; tail -4 /work/out.txt | sed 's/^/    /'; echo "    exit=$rc"; }
S="$ORG/$PROJECT/$STACK"
URN="urn:pulumi:$STACK::$PROJECT"

mark login;        run pulumi login "$BACKEND"
mark select;       if [ "$MODE" = stub ]; then run pulumi stack init "$S"; else run pulumi stack select "$S"; fi
mark up;           run pulumi up --yes
mark up-long;      CAPTURE_SLEEP="$LONG" run pulumi up --yes --refresh
mark history;      run pulumi stack history --json
mark tag-set;      run pulumi stack tag set capture yes
mark tag-ls;       run pulumi stack tag ls
mark tag-rm;       run pulumi stack tag rm capture
mark protect;      run pulumi state protect "$URN::random:index/randomString:RandomString::s" --yes
mark unprotect;    run pulumi state unprotect "$URN::random:index/randomString:RandomString::s" --yes
mark state-delete; run pulumi state delete "$URN::random:index/randomPet:RandomPet::d" --yes
cat > /work/edit.sh <<'EOF'
#!/bin/sh
sed -i 's/"length": 8/"length": 8/' "$1"
EOF
chmod +x /work/edit.sh
mark state-edit
if command -v script >/dev/null; then
  EDITOR=/work/edit.sh script -qec "pulumi state edit" /dev/null > /work/out.txt 2>&1; echo "    exit=$? (under a pty)"; tail -3 /work/out.txt | sed 's/^/    /'
else
  echo "    no pty available: state edit refuses non-interactive mode"
fi
mark secrets-passphrase; run pulumi stack change-secrets-provider passphrase
mark secrets-service;    run pulumi stack change-secrets-provider default
mark secrets-awskms;     run pulumi stack change-secrets-provider "awskms://alias/terrapod-capture?region=eu-west-1"
mark rename;       run pulumi stack rename "$ORG/$PROJECT/${STACK}2"
mark rename-back;  run pulumi stack rename "$ORG/$PROJECT/$STACK"
mark cancel
( CAPTURE_SLEEP=60 pulumi up --yes --non-interactive > /work/bg.txt 2>&1 ) &
sleep 20
run pulumi cancel --yes
wait
tail -3 /work/bg.txt | sed 's/^/    [bg up] /'
mark destroy;      run pulumi destroy --yes
mark done
"""


def _normalise(path: str) -> str:
    p = path[len(PREFIX) :] if path.startswith(PREFIX) else path
    p = re.sub(r"/api/stacks/[^/]+/[^/]+/[^/]+", "/api/stacks/{stack}", p)
    p = re.sub(
        r"/(update|preview|refresh|destroy|import)/[0-9a-zA-Z-]{8,}", r"/\1/{id}", p
    )
    return re.sub(r"/api/user/organizations/[^/]+", "/api/user/organizations/{org}", p)


def _api(
    base: str,
    token: str,
    ctx: ssl.SSLContext | None,
    method: str,
    path: str,
    body: dict | None = None,
) -> tuple[int, dict]:
    req = urllib.request.Request(
        base + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/vnd.api+json",
        },
    )
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", "replace")[:300]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--backend", default="", help="a live Terrapod base URL; omit for the stub"
    )
    ap.add_argument("--token", default="", help="a Terrapod API token for --backend")
    ap.add_argument(
        "--cafile", default="", help="CA bundle for --backend (default: mkcert -CAROOT)"
    )
    ap.add_argument(
        "--long",
        type=int,
        default=0,
        help="seconds the long update sleeps (stub: 45, live: 200)",
    )
    args = ap.parse_args()
    if shutil.which("docker") is None:
        print("docker is required", file=sys.stderr)
        return 2

    live = bool(args.backend)
    ctx = None
    ws_id = None
    if live:
        cafile = args.cafile
        if not cafile and shutil.which("mkcert"):
            root = subprocess.run(
                ["mkcert", "-CAROOT"], capture_output=True, text=True, check=False
            ).stdout.strip()
            cafile = str(pathlib.Path(root) / "rootCA.pem")
        ctx = ssl.create_default_context(cafile=cafile or None)
        Handler.upstream = urllib.parse.urlsplit(args.backend.rstrip("/"))
        Handler.ssl_ctx = ctx
        st, doc = _api(
            args.backend.rstrip("/"),
            args.token,
            ctx,
            "POST",
            "/api/v1/workspaces",
            {
                "data": {
                    "type": "workspaces",
                    "attributes": {"name": f"{PROJECT}::capture", "engine": "pulumi"},
                }
            },
        )
        if st != 201:
            print(
                f"could not create the capture workspace: {st} {doc}", file=sys.stderr
            )
            return 1
        ws_id = doc["data"]["id"]
        print(f"created workspace {PROJECT}::capture ({ws_id})")

    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)

    work = pathlib.Path(tempfile.mkdtemp(prefix="pulumi-secondary-"))
    (work / "proj").mkdir()
    (work / "proj" / "Pulumi.yaml").write_text(f"name: {PROJECT}\nruntime: python\n")
    (work / "proj" / "__main__.py").write_text(PROGRAM)
    (work / "run.sh").write_text(SCRIPT)
    base = f"http://{CONTAINER_HOST}:{PORT}"
    env = {
        "MODE": "live" if live else "stub",
        "BACKEND": base + PREFIX,
        "MARK": base,
        "PULUMI_TOKEN": args.token if live else "pul-capture",
        "ORG": "default" if live else "spike",
        "PROJECT": PROJECT,
        "STACK": "capture",
        "LONG": str(args.long or 200),
    }
    cmd = ["docker", "run", "--rm", "-v", f"{work}:/work", "-w", "/work"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    proc = subprocess.run(
        cmd + [IMAGE, "bash", "/work/run.sh"],
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
    )
    srv.shutdown()
    print(proc.stdout)
    if proc.returncode:
        print(proc.stderr[-2000:], file=sys.stderr)

    if live and ws_id:
        st, _ = _api(
            args.backend.rstrip("/"),
            args.token,
            ctx,
            "DELETE",
            f"/api/v1/workspaces/{ws_id}",
        )
        print(f"deleted capture workspace: {st}")

    print(f"=== {'live' if live else 'stub'}: {len(LOG)} requests ===")
    last = None
    for step, method, path, status in LOG:
        if step != last:
            print(f"-- {step}")
            last = step
        print(f"   {method:6s} {_normalise(path):60s} {status}")
    print("=== request bodies of the secondary routes ===")
    for step, method, path, body in BODIES:
        print(f"   [{step}] {method} {_normalise(path)}  {body}")
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
