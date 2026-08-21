#!/usr/bin/env python3
"""Mint a Terrapod API token by driving the real login flow.

Prints the raw token on stdout and nothing else, so it can be captured directly:

    TOKEN="$(python3 scripts/mint-token.py --url http://localhost:3000 \
             --email admin@terrapod.local --password 'TestPassword123!')"

Used by scripts/oci-conformance.sh, and useful by hand against a dev stack. It
goes through the ordinary PKCE authorization-code exchange rather than reaching
into the database, so a break in the login path shows up here rather than being
masked by a shortcut.

Standard library only — it runs on a CI runner with no Terrapod dependencies
installed, and on any machine with Python.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _post(url: str, body: bytes, content_type: str) -> dict:
    request = urllib.request.Request(  # noqa: S310 — the URL is an operator argument
        url, data=body, headers={"Content-Type": content_type}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Base URL of the Terrapod deployment")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    args = parser.parse_args()

    base = args.url.rstrip("/")
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())

    try:
        authorized = _post(
            f"{base}/api/terrapod/v1/auth/local/authorize",
            json.dumps(
                {
                    "email": args.email,
                    "password": args.password,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "state": _b64url(secrets.token_bytes(8)),
                }
            ).encode(),
            "application/json",
        )
        token = _post(
            f"{base}/oauth/token",
            urllib.parse.urlencode(
                {
                    "grant_type": "authorization_code",
                    "code": authorized["code"],
                    "code_verifier": verifier,
                    "client_id": "terraform-cli",
                    "redirect_uri": "",
                }
            ).encode(),
            "application/x-www-form-urlencoded",
        )
    except urllib.error.HTTPError as exc:
        # To stderr, so a caller capturing stdout gets an empty token rather
        # than an error message it would then try to authenticate with.
        print(f"mint-token: {exc.code} {exc.reason}: {exc.read().decode()}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"mint-token: could not reach {base}: {exc.reason}", file=sys.stderr)
        return 1

    print(token["access_token"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
