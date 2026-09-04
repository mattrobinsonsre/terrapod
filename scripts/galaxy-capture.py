#!/usr/bin/env python3
"""Capture the Galaxy surface `ansible-galaxy` actually consumes (#1482).

Stands up a request-logging stub that answers just enough Galaxy v3 to keep the
client walking forward, drives the real `ansible-galaxy` through install and
publish, and prints every request it made. The output is what
`docs/galaxy-cli-surface.md` is written from.

This is committed rather than thrown away so a future `ansible-core` can be
**re-captured rather than re-guessed**. The capture has already contradicted the
obvious reading of the docs twice — the collection path is the short
`/api/v3/collections/…` form and not the `plugin/ansible/content/published` one,
and the publish response's `task` URL is not followed but mined for its last
path segment. Neither is something to take on trust across a client upgrade.

    python3 scripts/galaxy-capture.py

Requires `ansible-galaxy` on PATH. Stdlib only otherwise; it must run anywhere.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"
NS, NAME, VER = "captns", "captcoll", "1.0.0"

LOG: list[dict] = []
STATE: dict[str, object] = {"artifact": "", "sha": "0" * 64, "size": 0, "task_url": ""}


class Handler(BaseHTTPRequestHandler):
    """Answers the minimum that keeps the client moving, and records everything."""

    def _record(self, body_len: int = 0) -> None:
        LOG.append(
            {
                "method": self.command,
                "path": self.path,
                "auth": bool(self.headers.get("Authorization")),
                "body_bytes": body_len,
            }
        )

    def _json(self, code: int, payload: object) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # stdlib naming, not ours to change
        self._record()
        p = self.path.split("?")[0].rstrip("/")

        if p in ("/api", "/api/v3"):
            return self._json(
                200, {"description": "capture", "available_versions": {"v3": "v3/"}}
            )

        if "/imports/" in p:
            return self._json(
                200, {"state": "completed", "finished_at": "2026-01-01T00:00:00Z"}
            )

        if re.search(rf"/collections(/index)?/{NS}/{NAME}$", p):
            return self._json(
                200,
                {
                    # `href` is not optional: omit it and the client dies with a
                    # bare KeyError reported as "probably a bug".
                    "href": f"{BASE}{p}/",
                    "namespace": NS,
                    "name": NAME,
                    "deprecated": False,
                    "versions_url": f"{BASE}{p}/versions/",
                    "highest_version": {"version": VER, "href": ""},
                },
            )

        if re.search(rf"/collections(/index)?/{NS}/{NAME}/versions$", p):
            return self._json(
                200,
                {
                    "meta": {"count": 1},
                    "links": {
                        "first": p + "/",
                        "last": p + "/",
                        "next": None,
                        "previous": None,
                    },
                    "data": [{"version": VER, "href": f"{BASE}{p}/{VER}/"}],
                },
            )

        if re.search(rf"/collections(/index)?/{NS}/{NAME}/versions/{VER}$", p):
            return self._json(
                200,
                {
                    "version": VER,
                    "href": f"{BASE}{p}/",
                    "namespace": {"name": NS},
                    "collection": {"name": NAME},
                    "artifact": {
                        "filename": f"{NS}-{NAME}-{VER}.tar.gz",
                        "sha256": STATE["sha"],
                        "size": STATE["size"],
                    },
                    "download_url": f"{BASE}/download/{NS}-{NAME}-{VER}.tar.gz",
                    "metadata": {"dependencies": {}, "tags": []},
                    "requires_ansible": ">=2.15",
                    "signatures": [],
                },
            )

        if p.startswith("/download/"):
            data = pathlib.Path(str(STATE["artifact"])).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return None

        return self._json(404, {"code": "not_found", "message": p})

    def do_POST(self) -> None:  # stdlib naming, not ours to change
        # Drain rather than buffer: a real collection artifact is not small.
        remaining = int(self.headers.get("Content-Length", 0) or 0)
        total = remaining
        while remaining > 0:
            remaining -= len(self.rfile.read(min(remaining, 65536)))
        self._record(total)
        # A deliberately unrelated task URL, so the capture shows whether the
        # client follows it or mines it for an id.
        return self._json(202, {"task": str(STATE["task_url"])})

    def log_message(self, *_args: object) -> None:
        return


def _build_collection(work: pathlib.Path, env: dict[str, str]) -> pathlib.Path:
    """Build a real collection with the real tool, so digests are genuine."""
    subprocess.run(
        [
            "ansible-galaxy",
            "collection",
            "init",
            f"{NS}.{NAME}",
            "--init-path",
            str(work),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        [
            "ansible-galaxy",
            "collection",
            "build",
            str(work / NS / NAME),
            "--output-path",
            str(work),
            "--force",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    tarball = work / f"{NS}-{NAME}-{VER}.tar.gz"
    if not tarball.exists():
        raise SystemExit(f"the collection did not build: {tarball}")
    return tarball


def main() -> int:
    if shutil.which("ansible-galaxy") is None:
        print("ansible-galaxy is not on PATH", file=sys.stderr)
        return 2

    work = pathlib.Path(tempfile.mkdtemp(prefix="galaxy-capture-"))
    env = {**os.environ, "ANSIBLE_FORCE_COLOR": "0"}

    tarball = _build_collection(work, env)
    STATE["artifact"] = str(tarball)
    STATE["sha"] = hashlib.sha256(tarball.read_bytes()).hexdigest()
    STATE["size"] = tarball.stat().st_size
    STATE["task_url"] = f"{BASE}/deliberately/unrelated/capture-id-1/"

    cfg = work / "ansible.cfg"
    cfg.write_text(
        f"[galaxy]\nserver_list = capture\n\n"
        f"[galaxy_server.capture]\nurl = {BASE}/api/\ntoken = capture-token\n"
    )
    env["ANSIBLE_CONFIG"] = str(cfg)

    srv = HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)

    def run(label: str, argv: list[str]) -> dict:
        # The client's own metadata cache will otherwise answer requests the
        # server never sees, which silently shortens the captured walk.
        shutil.rmtree(
            pathlib.Path.home() / ".ansible" / "galaxy_cache", ignore_errors=True
        )
        LOG.clear()
        # check=False deliberately: a non-zero rc is a capture result to report,
        # not an error to raise — an incomplete walk is exactly what we want to see.
        proc = subprocess.run(
            argv, env=env, capture_output=True, text=True, timeout=300, check=False
        )
        return {
            "label": label,
            "rc": proc.returncode,
            "requests": list(LOG),
            "stderr": [
                l
                for l in (proc.stderr or "").splitlines()
                if "unexpected" in l or "ERROR" in l
            ][-4:]
            or (proc.stderr or proc.stdout).strip().splitlines()[-4:],
        }

    steps = [
        run(
            "install",
            [
                "ansible-galaxy",
                "collection",
                "install",
                f"{NS}.{NAME}",
                "-p",
                str(work / "collections"),
                "--force",
            ],
        ),
        run("publish", ["ansible-galaxy", "collection", "publish", str(tarball)]),
    ]
    srv.shutdown()

    failed = False
    for step in steps:
        print(f"=== {step['label']} (rc={step['rc']}) ===")
        if step["rc"] != 0:
            failed = True
        for q in step["requests"]:
            body = f"  [{q['body_bytes']}B body]" if q["body_bytes"] else ""
            auth = "auth" if q["auth"] else "anon"
            print(f"  {q['method']:5s} {q['path']}  ({auth}){body}")
        if step["rc"] != 0:
            for line in step["stderr"]:
                print(f"  ! {line}")
    shutil.rmtree(work, ignore_errors=True)

    if failed:
        print("\na step failed — the capture is incomplete", file=sys.stderr)
        return 1
    print(
        "\nCompare against docs/galaxy-cli-surface.md; update it if the client has moved."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
