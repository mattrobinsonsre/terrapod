#!/usr/bin/env python3
"""Seed the presentable estate the documentation screenshots are taken against.

The screenshots in `docs/images/` are the first thing anyone sees of Terrapod,
and they are only as good as the data behind them. A development instance
accumulates fixture workspaces with names like `proof-1933-badpw-164240`, which
say nothing to a reader and make the product look like a test harness -- so the
images are captured against this seeded estate instead, not against whatever
happens to be on the instance.

It is IDEMPOTENT: an existing workspace of the same name is updated rather than
duplicated, so it can be re-run before every capture and the estate converges.
Nothing is deleted -- anything already on the instance is left exactly as it is,
because on a developer's stack those rows are somebody's work in progress.

    python3 scripts/docs-screenshots/seed_demo_estate.py --url https://terrapod.local
    cd e2e && node capture-docs-screenshots.mjs --url https://terrapod.local \
                     --out ../docs/images

The capture half lives in `e2e/` because node resolves an ESM import from
the script's own directory, so a Playwright script outside that tree cannot
find @playwright/test however it is invoked.

The token is read from `~/.terraform.d/credentials.tfrc.json`, i.e. whatever
`tofu login` last wrote, so the script holds no credential of its own.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import ssl
import sys
import urllib.error
import urllib.request

# A small estate that exercises what the product actually does: both engines,
# both execution modes, a configure-capable workspace, and labels that look like
# an organisation's rather than a test's. Names are region-suffixed because that
# is how real estates are named and it makes the list read as a fleet.
POOL = "dev-pool"
ESTATE = [
    # name, engine, mode, cpu, mem, labels, extra
    (
        "checkout-prod-eu-west-1",
        "terraform",
        "agent",
        "2",
        "4Gi",
        {"env": "prod", "team": "payments", "tier": "critical"},
        {},
    ),
    (
        "checkout-staging-eu-west-1",
        "terraform",
        "local",
        "1",
        "2Gi",
        {"env": "staging", "team": "payments"},
        {},
    ),
    (
        "data-platform-dev-eu-west-1",
        "terraform",
        "local",
        "1",
        "2Gi",
        {"env": "dev", "team": "data"},
        {},
    ),
    (
        "data-platform-prod-eu-west-1",
        "terraform",
        "agent",
        "4",
        "8Gi",
        {"env": "prod", "team": "data", "tier": "critical"},
        {},
    ),
    (
        "identity-prod-eu-west-1",
        "terraform",
        "agent",
        "2",
        "4Gi",
        {"env": "prod", "team": "platform", "tier": "critical"},
        {},
    ),
    (
        "identity-staging-eu-west-1",
        "terraform",
        "local",
        "1",
        "2Gi",
        {"env": "staging", "team": "platform"},
        {},
    ),
    (
        "observability-prod-eu-west-1",
        "terraform",
        "agent",
        "2",
        "4Gi",
        {"env": "prod", "team": "platform"},
        {},
    ),
    (
        "payments-api-prod-eu-west-1",
        "terraform",
        "agent",
        "2",
        "4Gi",
        {"env": "prod", "team": "payments", "tier": "critical"},
        {},
    ),
    ("sandbox-dev-eu-west-1", "terraform", "local", "1", "2Gi", {"env": "dev"}, {}),
    # A Pulumi workspace. The name MUST be `project::stack`: the project half is
    # matched against `name:` in Pulumi.yaml, so an ordinary name cannot select
    # a stack at all.
    ("edge-cdn::prod", "pulumi", "agent", "2", "4Gi", {"env": "prod", "team": "platform"}, {}),
    # A configure-capable workspace. `config-directory` is what makes the
    # Configure operation exist -- NULL means no configure, and "" means the
    # repository root, so the two are not interchangeable.
    (
        "fleet-config-prod-eu-west-1",
        "terraform",
        "agent",
        "2",
        "4Gi",
        {"env": "prod", "team": "platform"},
        {"config-directory": "ansible", "configure-enabled": True},
    ),
]


def token() -> str:
    p = pathlib.Path.home() / ".terraform.d" / "credentials.tfrc.json"
    if not p.exists():
        sys.exit(f"no credentials at {p} -- run `tofu login <host>` first")
    creds = json.loads(p.read_text())["credentials"]
    for host in creds:
        return creds[host]["token"]
    sys.exit("no host in the credentials file")


def call(url: str, tok: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{url}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Accept", "application/vnd.api+json")
    if data:
        req.add_header("Content-Type", "application/vnd.api+json")
    # A development instance uses a locally-trusted certificate; this script is
    # pointed at one by hand and never at anything else.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, context=ctx) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:300].decode(errors="replace")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://terrapod.local")
    args = ap.parse_args()
    url, tok = args.url.rstrip("/"), token()

    status, pools = call(url, tok, "GET", "/api/v1/agent-pools")
    if status != 200:
        sys.exit(f"cannot list agent pools: {status} {pools}")
    pool_id = next(
        (p["id"] for p in pools.get("data", []) if p["attributes"].get("name") == POOL), None
    )
    if pool_id is None:
        sys.exit(f"no agent pool named {POOL!r} on this instance")

    status, existing = call(url, tok, "GET", "/api/v1/workspaces?page%5Bsize%5D=0")
    by_name = {w["attributes"]["name"]: w["id"] for w in existing.get("data", [])}

    created = updated = failed = 0
    for name, engine, mode, cpu, mem, labels, extra in ESTATE:
        attrs = {
            "name": name,
            "engine": engine,
            "execution-mode": mode,
            "resource-cpu": cpu,
            "resource-memory": mem,
            "labels": labels,
            **extra,
        }
        if mode == "agent":
            attrs["agent-pool-ids"] = [pool_id]
        if name in by_name:
            # PATCH takes no `name`: it is the identity, not a setting.
            patch = {k: v for k, v in attrs.items() if k not in ("name", "engine")}
            st, body = call(
                url,
                tok,
                "PATCH",
                f"/api/v1/workspaces/{by_name[name]}",
                {"data": {"type": "workspaces", "attributes": patch}},
            )
            ok = st in (200, 204)
            updated += ok
        else:
            st, body = call(
                url,
                tok,
                "POST",
                "/api/v1/workspaces",
                {"data": {"type": "workspaces", "attributes": attrs}},
            )
            ok = st in (200, 201)
            created += ok
        if not ok:
            failed += 1
            detail = ""
            if isinstance(body, dict):
                errs = body.get("errors") or []
                detail = errs[0].get("detail", "") if errs else body.get("detail", "")
            print(f"  FAILED {name}: {st} {detail}")
        else:
            print(f"  {'updated' if name in by_name else 'created'} {name}")

    print(f"\n  {created} created, {updated} updated, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
