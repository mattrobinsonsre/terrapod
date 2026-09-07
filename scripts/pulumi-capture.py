#!/usr/bin/env python3
"""Capture what `pulumi plugin install` asks a download-URL override for (#1483).

Same method as `scripts/galaxy-capture.py`, and for the same reason: the Galaxy
work turned up five things the documentation would have had us build wrong, two
of which no synthetic client would have caught. A protocol is catalogued from the
client before it is implemented.

Stands up a request-logging stub, points `PULUMI_PLUGIN_DOWNLOAD_URL_OVERRIDES`
at it, runs a real `pulumi plugin install`, and prints every request. The output
is what `docs/pulumi-cli-surface.md` is written from.

    python3 scripts/pulumi-capture.py [path-to-pulumi]

Requires the `pulumi` CLI. Stdlib only otherwise, so it runs anywhere.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8804
BASE = f"http://127.0.0.1:{PORT}"

LOG: list[dict] = []
STATE: dict[str, object] = {"payload": b""}


class Handler(BaseHTTPRequestHandler):
    """Serves a plausible plugin tarball for anything, and records the asking."""

    def _record(self) -> None:
        LOG.append(
            {
                "method": self.command,
                "path": self.path,
                "accept": self.headers.get("Accept", ""),
                "ua": (self.headers.get("User-Agent", "") or "")[:40],
            }
        )

    def do_GET(self) -> None:  # stdlib naming, not ours to change
        self._record()
        payload = STATE["payload"]
        assert isinstance(payload, bytes)
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_HEAD(self) -> None:  # stdlib naming
        self._record()
        payload = STATE["payload"]
        assert isinstance(payload, bytes)
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        return


def _fake_plugin(work: pathlib.Path) -> bytes:
    """A tarball shaped like a resource plugin.

    The CLI unpacks what it downloads and expects an executable named for the
    plugin, so a tarball of nothing gets rejected before we learn anything about
    the request sequence.
    """
    stage = work / "stage"
    stage.mkdir()
    binary = stage / "pulumi-resource-random"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    out = work / "plugin.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(binary, arcname=binary.name)
    return out.read_bytes()


def main() -> int:
    pulumi = sys.argv[1] if len(sys.argv) > 1 else shutil.which("pulumi")
    if not pulumi or not os.path.exists(pulumi):
        print("pulumi not found; pass its path as the first argument", file=sys.stderr)
        return 2

    work = pathlib.Path(tempfile.mkdtemp(prefix="pulumi-capture-"))
    STATE["payload"] = _fake_plugin(work)

    srv = HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)

    env = {
        **os.environ,
        # The whole mechanism under test: a comma-separated `pattern=url` list.
        "PULUMI_PLUGIN_DOWNLOAD_URL_OVERRIDES": f"^random$={BASE}",
        # Keep the run away from the developer's real plugin cache, so a plugin
        # already installed cannot make the capture look shorter than it is.
        "PULUMI_HOME": str(work / "home"),
        "PULUMI_SKIP_UPDATE_CHECK": "true",
    }

    proc = subprocess.run(
        [pulumi, "plugin", "install", "resource", "random", "4.16.3"],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    srv.shutdown()

    print(f"=== plugin install (rc={proc.returncode}) ===")
    for entry in LOG:
        print(f"  {entry['method']:5s} {entry['path']}")
    if proc.returncode != 0:
        for line in (proc.stderr or proc.stdout).strip().splitlines()[-4:]:
            print(f"  ! {line}")
    shutil.rmtree(work, ignore_errors=True)

    print("\nCompare against docs/pulumi-cli-surface.md; update it if the client has moved.")
    return 0 if LOG else 1


if __name__ == "__main__":
    raise SystemExit(main())
