"""Seed a Terrapod instance with the documentation demo estate (#720).

The screenshots in `docs/` are only as convincing as the data behind them. A
platform shown with three workspaces called `test-1` and a four-resource plan
reads as a prototype no matter how good the UI is, so this seeds an estate that
looks like one in use: recognisable AWS resources, a plan containing a create,
an in-place update, a replace and destroys, and enough sibling workspaces that
a list view has something to show.

It NEVER touches AWS. See `fixture/main.tf` — the provider runs on mock
credentials with every validation and metadata call skipped, so a plan expands
real provider schema entirely offline. The checked-in `demo.tfstate` is what
gives the plan its destroys and its in-place update.

Because the run is REAL, everything downstream of it is real too: the plan log,
the plan JSON, the cost estimate, the Checkov findings and the OPA results are
all genuine output rather than staged screenshots.

    scripts/demo/seed.py --host terrapod.local [--token …] [--clean]

The token is read from --token, then $TERRAPOD_TOKEN, then the host's entry in
~/.terraform.d/credentials.tfrc.json (i.e. whatever `tofu login` left).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import pathlib
import ssl
import tarfile
import time
import urllib.error
import urllib.request

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixture"

# A workspace list with one row proves nothing. These are the supporting cast:
# plausible service/environment names across regions, seeded so the list, the
# label browser and the estate view have realistic shape.
# Each entry carries the shape the list actually renders, because a table where
# every row reads "local / — / — / 1 CPU / 2Gi" looks like an unused install no
# matter how good the product is. Real estates are uneven: production runs on
# agents with more memory, dev sits on the CLI, and only some workspaces have
# run recently.
#
#   agent  — agent execution on the pool (prod/critical work)
#   cpu/mem— per-workspace runner sizing (K8s quantity strings)
#   run    — "changes"  execute a real plan that finds drift
#            "broken"   execute a real plan that fails, because a real estate
#                       always has one, and an errored row is worth showing
#            None       never run
ESTATE = [
    (
        "checkout-prod-eu-west-1",
        {"team": "payments", "env": "prod", "tier": "critical"},
        {"agent": True, "cpu": "2", "mem": "4Gi", "run": "changes"},
    ),
    (
        "checkout-staging-eu-west-1",
        {"team": "payments", "env": "staging"},
        {"agent": False, "cpu": "1", "mem": "2Gi", "run": None},
    ),
    (
        "payments-api-prod-eu-west-1",
        {"team": "payments", "env": "prod", "tier": "critical"},
        {"agent": True, "cpu": "2", "mem": "4Gi", "run": "changes"},
    ),
    (
        "identity-prod-eu-west-1",
        {"team": "platform", "env": "prod", "tier": "critical"},
        {"agent": True, "cpu": "2", "mem": "4Gi", "run": "broken"},
    ),
    (
        "identity-staging-eu-west-1",
        {"team": "platform", "env": "staging"},
        {"agent": False, "cpu": "1", "mem": "2Gi", "run": None},
    ),
    (
        "search-prod-us-east-1",
        {"team": "discovery", "env": "prod"},
        {"agent": True, "cpu": "2", "mem": "4Gi", "run": "changes"},
    ),
    (
        "search-staging-us-east-1",
        {"team": "discovery", "env": "staging"},
        {"agent": False, "cpu": "1", "mem": "2Gi", "run": None},
    ),
    (
        "data-platform-prod-eu-west-1",
        {"team": "data", "env": "prod", "tier": "critical"},
        {"agent": True, "cpu": "4", "mem": "8Gi", "run": "changes"},
    ),
    (
        "data-platform-dev-eu-west-1",
        {"team": "data", "env": "dev"},
        {"agent": False, "cpu": "1", "mem": "2Gi", "run": None},
    ),
    (
        "shared-network-prod-eu-west-1",
        {"team": "platform", "env": "prod", "tier": "critical"},
        {"agent": True, "cpu": "2", "mem": "4Gi", "run": None},
    ),
    (
        "observability-prod-eu-west-1",
        {"team": "platform", "env": "prod"},
        {"agent": True, "cpu": "2", "mem": "4Gi", "run": None},
    ),
    (
        "sandbox-dev-eu-west-1",
        {"team": "platform", "env": "dev"},
        {"agent": False, "cpu": "1", "mem": "2Gi", "run": None},
    ),
]

HERO = ESTATE[0][0]
POLICY_SET = "production-guardrails"


class Api:
    def __init__(self, host: str, token: str, insecure: bool):
        self.base = f"https://{host}"
        self.token = token
        self.ctx = ssl._create_unverified_context() if insecure else None

    def _req(
        self,
        method: str,
        path: str,
        body=None,
        ctype="application/vnd.api+json",
        auth=True,
        conflict_ok: bool = False,
    ):
        url = path if path.startswith("http") else self.base + path
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method)
        if auth:
            req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=120) as r:
                raw = r.read()
                return json.loads(raw) if raw and raw[:1] in (b"{", b"[") else raw
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            # Seeding is expected to be re-run before every capture, and several
            # of these creates are one-per-workspace. A 409 means the thing is
            # already there, which for a seeder is success, not failure.
            if e.code == 409 and conflict_ok:
                return None
            raise SystemExit(f"{method} {path} -> {e.code}: {detail}") from None

    get = lambda self, p: self._req("GET", p)  # noqa: E731
    post = lambda self, p, b=None, conflict_ok=False: self._req(  # noqa: E731
        "POST", p, b, conflict_ok=conflict_ok
    )
    delete = lambda self, p: self._req("DELETE", p)  # noqa: E731


def resolve_token(args) -> str:
    if args.token:
        return args.token
    if os.environ.get("TERRAPOD_TOKEN"):
        return os.environ["TERRAPOD_TOKEN"]
    creds = pathlib.Path.home() / ".terraform.d" / "credentials.tfrc.json"
    if creds.exists():
        try:
            tok = json.loads(creds.read_text())["credentials"][args.host]["token"]
            if tok:
                return tok
        except (KeyError, json.JSONDecodeError):
            pass
    raise SystemExit(f"No token. Pass --token, set $TERRAPOD_TOKEN, or run: tofu login {args.host}")


BROKEN_TF = """# Deliberately invalid: the module below is never published, so `init` fails.
# Every real estate has a workspace in this state, and a screenshot where
# nothing has ever gone wrong is not a screenshot of a real system.
module "vpc" {
  source  = "app.terraform.io/example/vpc/aws"
  version = "9.9.9"
}
"""


def tarball(variant: str = "changes") -> bytes:
    """The fixture as a config-version tarball, state file excluded."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if variant == "broken":
            info = tarfile.TarInfo("main.tf")
            data = BROKEN_TF.encode()
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        else:
            for tf in sorted(FIXTURE.glob("*.tf")):
                tar.add(tf, arcname=tf.name)
    return buf.getvalue()


def pick_pool(api: Api) -> str:
    """A pool with a live listener, or the run will queue forever."""
    pools = api.get("/api/terrapod/v1/agent-pools").get("data", [])
    live = [p for p in pools if (p["attributes"].get("listener-count") or 0) > 0]
    if not live:
        raise SystemExit(
            "No agent pool has a live listener — a real plan cannot execute.\n"
            "Bring the stack up (make dev) and wait for the listener to join."
        )
    return live[0]["id"]


def find_workspace(api: Api, name: str):
    ws = api.get("/api/v2/organizations/default/workspaces?page%5Bsize%5D=200").get("data", [])
    return next((w for w in ws if w["attributes"]["name"] == name), None)


def create_workspace(api: Api, name: str, labels: dict, opts: dict, pool_id: str | None):
    existing = find_workspace(api, name)
    if existing:
        print(f"  = {name} (exists)")
        return existing["id"]
    attrs = {
        "name": name,
        "description": "Demo estate for documentation screenshots",
        "labels": labels,
        "auto-apply": False,
        "resource-cpu": opts["cpu"],
        "resource-memory": opts["mem"],
    }
    if opts["agent"] and pool_id:
        attrs.update(
            {
                "execution-mode": "agent",
                "agent-pool-id": pool_id,
                "terraform-version": "1.12.5",
                "execution-backend": "tofu",
            }
        )
    body = {"data": {"type": "workspaces", "attributes": attrs}}
    ws = api.post("/api/v2/organizations/default/workspaces", body)
    print(f"  + {name}")
    return ws["data"]["id"]


def seed_policy_set(api: Api):
    """An OPA policy set scoped to prod, with one policy the fixture fails.

    A policy screen showing nothing but passes proves the feature runs; one
    showing a real violation, naming a real resource address, proves it bites.
    The fixture's web tier opens 0.0.0.0/0 on purpose, so the failure is
    genuine — and it is the same rule the Checkov scan independently flags,
    which is a fair demonstration of the two layers agreeing.
    """
    existing = api.get("/api/terrapod/v1/policy-sets").get("data", [])
    if any(p["attributes"].get("name") == POLICY_SET for p in existing):
        print(f"  = policy set {POLICY_SET} (exists)")
        return
    ps = api.post(
        "/api/terrapod/v1/policy-sets",
        {
            "data": {
                "type": "policy-sets",
                "attributes": {
                    "name": POLICY_SET,
                    "description": "Guardrails applied to every production workspace",
                    "enforcement-level": "advisory",
                    "global-scope": False,
                    "allow-labels": {"env": ["prod"]},
                },
            }
        },
    )
    ps_id = ps["data"]["id"]
    for rego in sorted((pathlib.Path(__file__).resolve().parent / "policies").glob("*.rego")):
        api.post(
            f"/api/terrapod/v1/policy-sets/{ps_id}/policies",
            {
                "data": {
                    "type": "policies",
                    "attributes": {"name": rego.stem.replace("_", "-"), "rego": rego.read_text()},
                }
            },
        )
        print(f"  + policy {rego.stem.replace('_', '-')}")
    print(f"  + policy set {POLICY_SET} (advisory, scoped to env=prod)")


def seed_variables(api: Api, ws_id: str):
    """Credentials the way a real user supplies them: workspace env vars.

    The fixture deliberately does NOT hardcode keys in the provider block — a
    hardcoded key is itself a scanner finding (CKV_AWS_41), and a demo of
    security scanning that trips it on its own fixture is not a good look. The
    values are mock and never leave the runner; every AWS validation and
    metadata call is skipped.

    Seeding them here has a second benefit: the variables screen then shows
    realistic content, including a masked secret, instead of `region = x`.
    """
    variables = [
        ("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE", "env", False),
        ("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "env", True),
        ("AWS_REGION", "eu-west-1", "env", False),
        ("TF_LOG", "INFO", "env", False),
    ]
    # Idempotent: seeding is expected to be re-run before a capture, and the
    # API rejects a duplicate key with a 409. Skip what is already there rather
    # than making the second run of the documented workflow fail.
    existing = {
        v["attributes"]["key"] for v in api.get(f"/api/v2/workspaces/{ws_id}/vars").get("data", [])
    }
    added = 0
    for key, value, category, sensitive in variables:
        if key in existing:
            continue
        added += 1
        api.post(
            f"/api/v2/workspaces/{ws_id}/vars",
            {
                "data": {
                    "type": "vars",
                    "attributes": {
                        "key": key,
                        "value": value,
                        "category": category,
                        "sensitive": sensitive,
                        "hcl": False,
                        "description": "Mock credential — this fixture never calls AWS"
                        if key.startswith("AWS_") and "KEY" in key
                        else "",
                    },
                }
            },
        )
    print(
        f"  · {added} variable(s) seeded, {len(existing & {v[0] for v in variables})} already present"
    )


def upload_state(api: Api, ws_id: str):
    """The state is what gives the plan its destroys and its in-place update."""
    raw = (FIXTURE / "demo.tfstate").read_bytes()
    doc = json.loads(raw)
    body = {
        "data": {
            "type": "state-versions",
            "attributes": {
                "serial": doc["serial"],
                "md5": hashlib.md5(raw).hexdigest(),
                "lineage": doc["lineage"],
            },
        }
    }
    sv = api.post(f"/api/v2/workspaces/{ws_id}/state-versions", body, conflict_ok=True)
    if sv is None:
        print(f"  · state already at serial {doc['serial']}")
        return
    sv_id = sv["data"]["id"]
    api._req(
        "PUT",
        f"/api/v2/state-versions/{sv_id}/content",
        raw,
        ctype="application/octet-stream",
        auth=False,
    )
    print(f"  · state uploaded (serial {doc['serial']}, {len(doc['resources'])} resources)")


def upload_config(api: Api, ws_id: str, variant: str = "changes") -> str:
    body = {"data": {"type": "configuration-versions", "attributes": {"auto-queue-runs": False}}}
    cv = api.post(f"/api/v2/workspaces/{ws_id}/configuration-versions", body)
    url = cv["data"]["attributes"]["upload-url"]
    api._req("PUT", url, tarball(variant), ctype="application/octet-stream", auth=False)
    print("  · config uploaded")
    return cv["data"]["id"]


def queue_run(
    api: Api,
    ws_id: str,
    cv_id: str,
    message: str = "Migrate the web tier to Fargate and retire the legacy tier",
) -> str:
    body = {
        "data": {
            "type": "runs",
            "attributes": {
                "plan-only": True,
                # Refresh would call the real AWS APIs for everything in state,
                # which mock credentials cannot satisfy — and the fixture's whole
                # premise is that it never reaches a cloud. Planning against the
                # recorded state is also what produces the destroys and the
                # in-place update.
                "refresh": False,
                "message": message,
            },
            "relationships": {
                "workspace": {"data": {"type": "workspaces", "id": ws_id}},
                "configuration-version": {"data": {"type": "configuration-versions", "id": cv_id}},
            },
        }
    }
    run = api.post("/api/v2/runs", body)
    rid = run["data"]["id"]
    print(f"  · run queued: {rid}")
    return rid


def wait_for_plan(api: Api, run_id: str, timeout: int = 900) -> str:
    """Block until the plan is done — the screenshots need its artifacts."""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        a = api.get(f"/api/v2/runs/{run_id}")["data"]["attributes"]
        st = a["status"]
        if st != last:
            print(f"    {st}")
            last = st
        if st in ("planned", "errored", "canceled", "discarded", "applied"):
            return st
        time.sleep(5)
    return last or "timeout"


def clean(api: Api):
    """Remove previously-seeded demo workspaces so a re-seed is repeatable."""
    names = {n for n, _, _ in ESTATE}
    for w in api.get("/api/v2/organizations/default/workspaces?page%5Bsize%5D=200").get("data", []):
        if w["attributes"]["name"] in names:
            api.delete(f"/api/terrapod/v1/workspaces/{w['id']}")
            print(f"  - {w['attributes']['name']}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="terrapod.local")
    p.add_argument("--token")
    p.add_argument(
        "--insecure",
        action="store_true",
        default=None,
        help="skip TLS verification (default on for terrapod.local)",
    )
    p.add_argument("--clean", action="store_true", help="delete seeded workspaces, then exit")
    p.add_argument("--no-run", action="store_true", help="seed data but do not execute the plan")
    args = p.parse_args()

    insecure = args.insecure if args.insecure is not None else args.host.endswith(".local")
    api = Api(args.host, resolve_token(args), insecure)

    if args.clean:
        print("Removing seeded workspaces:")
        clean(api)
        return 0

    pool_id = None if args.no_run else pick_pool(api)
    print(f"Seeding the demo estate on {args.host}:")

    hero_id = None
    to_run: list[tuple[str, str, str]] = []  # (name, ws_id, variant)
    for name, labels, opts in ESTATE:
        ws_id = create_workspace(api, name, labels, opts, pool_id)
        if name == HERO:
            hero_id = ws_id
        if opts["run"]:
            to_run.append((name, ws_id, opts["run"]))

    if args.no_run:
        print("\nSeeded (no run). Re-run without --no-run to execute the hero plan.")
        return 0

    seed_policy_set(api)

    # Queue every run first, then wait. They execute on separate workspaces so
    # nothing serialises them, and waiting one-at-a-time would turn a couple of
    # minutes into ten.
    MESSAGES = {
        "checkout-prod-eu-west-1": "Migrate the web tier to Fargate and retire the legacy tier",
        "payments-api-prod-eu-west-1": "Raise the settlement worker pool for Black Friday",
        "search-prod-us-east-1": "Move the index cluster onto gp3 volumes",
        "data-platform-prod-eu-west-1": "Rotate the warehouse credentials and resize the writer",
        "identity-prod-eu-west-1": "Adopt the shared VPC module",
    }
    queued = []
    for name, ws_id, variant in to_run:
        print(f"\n{name}:")
        seed_variables(api, ws_id)
        upload_state(api, ws_id)
        cv_id = upload_config(api, ws_id, variant)
        rid = queue_run(api, ws_id, cv_id, MESSAGES.get(name, "Scheduled reconcile"))
        queued.append((name, ws_id, variant, rid))

    print(f"\nWaiting on {len(queued)} plan(s):")
    hero_run = None
    for name, _ws_id, variant, rid in queued:
        status = wait_for_plan(api, rid)
        want = "errored" if variant == "broken" else "planned"
        mark = "ok" if status == want else f"WANTED {want}"
        print(f"  {name:32} {status:10} {mark}")
        if name == HERO:
            hero_run, hero_status = rid, status

    if hero_status != "planned":
        print(f"\nThe hero plan finished as '{hero_status}', not 'planned'.")
        print("The screenshots need a completed plan — check the run's log before capturing.")
        return 1

    a = api.get(f"/api/v2/runs/{hero_run}")["data"]["attributes"]
    print(f"\nHero planned. has-changes={a.get('has-changes')}")
    print(
        f"  {args.host}/workspaces/{hero_id.removeprefix('ws-')}/runs/{hero_run.removeprefix('run-')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
