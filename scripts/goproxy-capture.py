#!/usr/bin/env python3
"""Capture what the Go toolchain asks a module proxy for (#1484).

Third in the series after `galaxy-capture.py` and `pulumi-capture.py`, for the
reason those two established: between them they turned up seven things the
documentation would have had us build wrong, and three were invisible to any
synthetic client.

Stands up a request-logging stub, points `GOPROXY` at it **under a path prefix**
— because that is an acceptance item, not an assumption — runs a real
`go mod download`, and prints every request.

    python3 scripts/goproxy-capture.py

Requires the `go` toolchain. Stdlib only otherwise.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8806
#: Deliberately not the host root. The Go client is documented as taking a base
#: URL, and this is where that gets proven rather than believed.
PREFIX = "/api/terrapod/v1/package-cache/go"
BASE = f"http://127.0.0.1:{PORT}{PREFIX}"

MODULE = "example.com/tinymod"
VERSION = "v1.0.0"

LOG: list[dict] = []
STATE: dict[str, bytes] = {}


def _build_module() -> None:
    """A real module zip, in the layout `go mod download` expects.

    The zip's paths must be `<module>@<version>/...` or the toolchain rejects it,
    so this is built rather than faked — a stub that 200s with nothing teaches
    us only that the client gives up.
    """
    mod = f"module {MODULE}\n\ngo 1.21\n"
    src = 'package tinymod\n\n// Hello is here so the package is not empty.\nfunc Hello() string { return "hi" }\n'

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{MODULE}@{VERSION}/go.mod", mod)
        zf.writestr(f"{MODULE}@{VERSION}/tinymod.go", src)

    STATE["mod"] = mod.encode()
    STATE["zip"] = buf.getvalue()
    STATE["info"] = json.dumps(
        {"Version": VERSION, "Time": "2026-01-01T00:00:00Z"}
    ).encode()
    STATE["list"] = f"{VERSION}\n".encode()


class Handler(BaseHTTPRequestHandler):
    def _record(self) -> None:
        LOG.append({"method": self.command, "path": self.path})

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # stdlib naming
        self._record()
        path = self.path

        if path.endswith("/@v/list"):
            self._send(STATE["list"], "text/plain")
            return
        if path.endswith("/@latest"):
            self._send(STATE["info"], "application/json")
            return
        if path.endswith(f"/@v/{VERSION}.info"):
            self._send(STATE["info"], "application/json")
            return
        if path.endswith(f"/@v/{VERSION}.mod"):
            self._send(STATE["mod"], "text/plain")
            return
        if path.endswith(f"/@v/{VERSION}.zip"):
            self._send(STATE["zip"], "application/zip")
            return

        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        return


def main() -> int:
    if shutil.which("go") is None:
        print("the go toolchain is not on PATH", file=sys.stderr)
        return 2

    _build_module()
    srv = HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)

    work = pathlib.Path(tempfile.mkdtemp(prefix="goproxy-capture-"))
    (work / "go.mod").write_text(
        f"module consumer\n\ngo 1.21\n\nrequire {MODULE} {VERSION}\n"
    )
    (work / "main.go").write_text(
        f'package main\n\nimport _ "{MODULE}"\n\nfunc main() {{}}\n'
    )

    env = {
        **os.environ,
        "GOPROXY": BASE,
        # The checksum database cannot know a made-up module, and reaching it
        # would be a second upstream this proxy does not serve — see the doc.
        "GONOSUMDB": "*",
        "GONOSUMCHECK": "1",
        "GOFLAGS": "-mod=mod",
        "GONOSUMVERIFY": "1",
        "GOSUMDB": "off",
        "GOPATH": str(work / "gopath"),
        "GOMODCACHE": str(work / "modcache"),
    }

    proc = subprocess.run(
        ["go", "mod", "download", MODULE],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    srv.shutdown()

    print(f"=== go mod download (rc={proc.returncode}) ===")
    for entry in LOG:
        print(f"  {entry['method']:5s} {entry['path']}")
    if proc.returncode != 0:
        for line in (proc.stderr or proc.stdout).strip().splitlines()[-5:]:
            print(f"  ! {line}")
    shutil.rmtree(work, ignore_errors=True)

    print(f"\nAll paths above are under {PREFIX}, which is the point:")
    print("GOPROXY takes a base URL, so the proxy need not sit at the host root.")
    return 0 if LOG else 1


if __name__ == "__main__":
    raise SystemExit(main())
