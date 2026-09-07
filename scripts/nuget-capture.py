#!/usr/bin/env python3
"""Capture what `dotnet restore` asks a NuGet V3 source for (#1484).

Fourth in the series, after Galaxy, Pulumi and the Go proxy. Between them those
turned up nine things the documentation would have had us build wrong, several
invisible to any synthetic client — which is why a protocol gets captured before
it gets implemented.

Stands up a request-logging stub, points a `nuget.config` package source at it
**under a path prefix**, runs a real `dotnet restore` in a container, and prints
every request.

    python3 scripts/nuget-capture.py

Requires Docker (for the .NET SDK image); stdlib only otherwise.

**The finding this exists to preserve:** the service index advertises absolute
`@id` URLs, and the client follows them verbatim. Advertise a URL the client
cannot reach — wrong host, or a path prefix dropped — and `restore` follows it
and gives up. Terrapod therefore builds the index per request from the caller's
own external base rather than storing one.
"""

from __future__ import annotations

import io
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8808
#: Deliberately not the host root — proving the proxy need not sit there is an
#: acceptance item of #1484, not an assumption.
PREFIX = "/api/terrapod/v1/package-cache/nuget"

PKG = "Tiny.Probe"
VER = "1.0.0"
LOG: list[str] = []

#: Reachable from inside the SDK container back to this host.
CONTAINER_HOST = "host.containers.internal"


def _nupkg() -> bytes:
    """A minimal but structurally valid package.

    Built rather than fixtured: a stub that 200s with arbitrary bytes teaches us
    only that the client rejects them.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{PKG}.nuspec",
            '<?xml version="1.0"?>\n<package><metadata>'
            f"<id>{PKG}</id><version>{VER}</version><authors>probe</authors>"
            "<description>probe</description>"
            "</metadata></package>",
        )
        zf.writestr("lib/netstandard2.0/_._", "")
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"><Default Extension="nuspec" ContentType="text/xml"/>'
            '<Default Extension="xml" ContentType="text/xml"/></Types>',
        )
    return buf.getvalue()


NUPKG = _nupkg()
BASE = f"http://{CONTAINER_HOST}:{PORT}{PREFIX}"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # stdlib naming
        LOG.append(self.path)
        path = self.path
        lower = PKG.lower()

        def send(body: bytes, ctype: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        if path == f"{PREFIX}/index.json":
            index = {
                "version": "3.0.0",
                "resources": [
                    {"@id": f"{BASE}/flat/", "@type": "PackageBaseAddress/3.0.0"}
                ],
            }
            return send(json.dumps(index).encode(), "application/json")
        if path == f"{PREFIX}/flat/{lower}/index.json":
            return send(json.dumps({"versions": [VER]}).encode(), "application/json")
        if path == f"{PREFIX}/flat/{lower}/{VER}/{lower}.{VER}.nupkg":
            return send(NUPKG, "application/octet-stream")

        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        return


def main() -> int:
    if shutil.which("docker") is None:
        print("docker is required (for the .NET SDK image)", file=sys.stderr)
        return 2

    srv = HTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)

    work = pathlib.Path(tempfile.mkdtemp(prefix="nuget-capture-"))
    (work / "p.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk">\n'
        "<PropertyGroup><TargetFramework>netstandard2.0</TargetFramework>"
        # Without this the restore also wants NETStandard.Library, which this
        # stub does not serve — a distraction from what is being captured.
        "<DisableImplicitNuGetFallbackFolder>true</DisableImplicitNuGetFallbackFolder>"
        "</PropertyGroup>\n"
        f'<ItemGroup><PackageReference Include="{PKG}" Version="{VER}" /></ItemGroup>\n'
        "</Project>"
    )
    (work / "nuget.config").write_text(
        '<?xml version="1.0"?>\n<configuration><packageSources><clear/>\n'
        f'<add key="terrapod" value="{BASE}/index.json" />\n'
        "</packageSources></configuration>"
    )

    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{work}:/p:z",
            "-w",
            "/p",
            "mcr.microsoft.com/dotnet/sdk:8.0",
            "dotnet",
            "restore",
            "--no-cache",
        ],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    srv.shutdown()

    print(f"=== dotnet restore (rc={proc.returncode}) ===")
    for path in LOG:
        print(f"  GET   {path}")
    if proc.returncode != 0:
        for line in (proc.stdout + proc.stderr).strip().splitlines()[-4:]:
            print(f"  ! {line[:160]}")
    shutil.rmtree(work, ignore_errors=True)

    print(f"\nEvery path is under {PREFIX}: the source URL is configuration, so")
    print("the proxy need not sit at the host root — and the service index's own")
    print("advertised @id must carry that prefix, or the client follows it nowhere.")
    return 0 if LOG else 1


if __name__ == "__main__":
    raise SystemExit(main())
