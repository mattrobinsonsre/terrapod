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
import subprocess
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
# `engine` and `extra` are optional. A Pulumi workspace MUST be created on the
# NATIVE surface: `_create_workspace_impl(..., engine=TERRAFORM)` pins the TFE
# route to Terraform by design, so a `pulumi` engine posted there is silently
# ignored. See `create_workspace` below.
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
    # ── The other two engines ────────────────────────────────────────────
    #
    # A documentation estate that is all Terraform cannot show what 2.0 is
    # about. These two exist so the workspace list carries an engine badge and
    # so the Ansible and Pulumi surfaces have something to photograph at all.
    (
        # Pulumi names a workspace `project::stack`, and the project half is
        # matched against `name:` in Pulumi.yaml -- an ordinary name cannot
        # select a stack.
        "edge-cdn::prod",
        {"team": "platform", "env": "prod"},
        {"agent": True, "cpu": "2", "mem": "4Gi", "run": None, "engine": "pulumi"},
    ),
    (
        # `config-directory` is what makes a Configure operation exist. NULL
        # means no configure and "" means the repository root, so the two are
        # not interchangeable.
        "fleet-config-prod-eu-west-1",
        {"team": "platform", "env": "prod"},
        {
            "agent": True,
            "cpu": "2",
            "mem": "4Gi",
            "run": None,
            "extra": {"config-directory": "ansible", "configure-enabled": True},
        },
    ),
]

HERO = ESTATE[0][0]
POLICY_SET = "production-guardrails"


def _tls_context() -> ssl.SSLContext:
    """A verifying TLS context that also trusts the local development CA.

    The obvious shortcut here is to skip verification, because the dev stack
    serves a certificate signed by a CA that is not in the system trust store.
    This used to do exactly that, for any host ending in `.local`, which is both
    a real weakness and unnecessary: mkcert is already a prerequisite for the dev
    stack and it installs a root CA whose path it will tell you. Trust that CA
    and verification simply works — no exception to make, and none to forget to
    take back out when the script is later pointed at something real.

    SSL_CERT_FILE wins if set, matching what the runner and the CLI already
    honour.
    """
    cafile = os.environ.get("SSL_CERT_FILE") or ""
    if not cafile:
        try:
            root = subprocess.run(
                ["mkcert", "-CAROOT"], capture_output=True, text=True, timeout=10, check=True
            ).stdout.strip()
            candidate = pathlib.Path(root) / "rootCA.pem"
            if candidate.is_file():
                cafile = str(candidate)
        except (OSError, subprocess.SubprocessError):
            pass  # No mkcert — fall back to the system trust store.
    return ssl.create_default_context(cafile=cafile or None)


class Api:
    def __init__(self, host: str, token: str):
        self.base = f"https://{host}"
        self.token = token
        self.ctx = _tls_context()

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
    # The NATIVE list, not the TFE one. `/api/v2` is engine-filtered by design
    # (#1905), so a Pulumi or Ansible workspace is invisible there -- and an
    # existence check that cannot see a workspace reports it as absent, then
    # tries to create it and gets a 422 naming the workspace it just failed to
    # find. The native surface serves every engine.
    ws = api.get("/api/v1/workspaces?page%5Bsize%5D=0").get("data", [])
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
                "engine-version": "1.12.5",
                "execution-backend": "tofu",
            }
        )
    attrs.update(opts.get("extra") or {})
    body = {"data": {"type": "workspaces", "attributes": attrs}}
    engine = opts.get("engine")
    if engine and engine != "terraform":
        # The TFE create route pins the engine to Terraform deliberately, so a
        # non-Terraform workspace has to be created natively or it comes back
        # as Terraform with no error.
        attrs["engine"] = engine
        ws = api.post("/api/v1/workspaces", body)
    else:
        ws = api.post("/api/v2/organizations/default/workspaces", body)
    print(f"  + {name}" + (f" ({engine})" if engine else ""))
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


def seed_vcs_connections(api: Api):
    """VCS connections, so the admin page shows the feature rather than its absence.

    The screenshot of this page went out reading "No VCS connections
    configured." -- clean, correct, and worthless to a reader, because the
    seeder never made one. That is the whole reason `assertClean` now refuses
    an empty state.

    GitLab rather than GitHub: a GitHub connection requires an App id, an
    installation id and a private key, and a PEM committed to a public
    repository looks exactly like a leaked key whether or not it is one. A
    GitLab connection needs a name and a token, so the fixture can be obviously
    fake. The tokens below are not credentials and are never used -- nothing in
    the demo estate polls these connections -- but they are deliberately not in
    the shape of a real `glpat-` token either, so no secret scanner has to
    decide.
    """
    wanted = [
        ("platform-monorepo", "https://gitlab.com"),
        ("service-templates", "https://gitlab.com"),
    ]
    existing = {
        c["attributes"].get("name") for c in api.get("/api/v1/vcs-connections").get("data", [])
    }
    for name, server in wanted:
        if name in existing:
            print(f"  = vcs connection {name} (exists)")
            continue
        api.post(
            "/api/v1/vcs-connections",
            {
                "data": {
                    "type": "vcs-connections",
                    "attributes": {
                        "name": name,
                        "provider": "gitlab",
                        "server-url": server,
                        "token": "demo-seed-value-not-a-real-token",
                    },
                }
            },
        )
        print(f"  + vcs connection {name} (gitlab)")


def seed_registry(api: Api):
    """A module and a provider, so the two registry pages show the registry.

    Both went out reading "No modules yet." / "No providers yet." -- the pages
    existed, rendered correctly and documented nothing. Neither needs an
    artifact to list: the module and provider rows are the namespace, and
    versions are published to them separately (a provider version needs
    client-side GPG signing, which a seeder has no business doing).
    """
    for path, kind, attrs in (
        ("/api/v1/registry-modules", "registry-modules", {"name": "vpc", "provider": "aws"}),
        (
            "/api/v1/registry-modules",
            "registry-modules",
            {"name": "ecs-service", "provider": "aws"},
        ),
        ("/api/v1/registry-providers", "registry-providers", {"name": "acme"}),
    ):
        label = attrs["name"] + (f"/{attrs['provider']}" if "provider" in attrs else "")
        existing = api.get(path).get("data", [])
        if any(
            e["attributes"].get("name") == attrs["name"]
            and e["attributes"].get("provider", attrs.get("provider"))
            == attrs.get("provider", e["attributes"].get("provider"))
            for e in existing
        ):
            print(f"  = registry {kind.split('-')[1]} {label} (exists)")
            continue
        api.post(path, {"data": {"type": kind, "attributes": attrs}})
        print(f"  + registry {kind.split('-')[1]} {label}")


def seed_autodiscovery(api: Api):
    """One autodiscovery rule, which needs a VCS connection to point at."""
    conns = api.get("/api/v1/vcs-connections").get("data", [])
    if not conns:
        print("  ! no VCS connection, skipping autodiscovery rule")
        return
    existing = api.get("/api/v1/autodiscovery-rules").get("data", [])
    if any(r["attributes"].get("name") == "prod-monorepo" for r in existing):
        print("  = autodiscovery rule prod-monorepo (exists)")
        return
    api.post(
        "/api/v1/autodiscovery-rules",
        {
            "data": {
                "type": "autodiscovery-rules",
                "attributes": {
                    "name": "prod-monorepo",
                    "vcs-connection-id": conns[0]["id"],
                    "repo-url": "https://gitlab.com/example/platform-monorepo",
                    "branch": "main",
                    "pattern": "environments/*/*",
                },
            }
        },
    )
    print("  + autodiscovery rule prod-monorepo")


def seed_workspace_integrations(api: Api, ws_id: str):
    """A notification and a run task on the hero workspace.

    Both tabs photographed as "No ... configured for this workspace." The URLs
    are example.com on purpose: nothing in the demo estate delivers to them,
    and a reachable endpoint in a committed fixture is a request somebody else
    eventually receives.
    """
    notifs = api.get(f"/api/v1/workspaces/{ws_id}/notification-configurations")
    if not any(n["attributes"].get("name") == "slack-prod" for n in notifs.get("data", [])):
        api.post(
            f"/api/v1/workspaces/{ws_id}/notification-configurations",
            {
                "data": {
                    "type": "notification-configurations",
                    "attributes": {
                        "name": "slack-prod",
                        "destination-type": "slack",
                        "url": "https://hooks.example.com/services/demo",
                        "triggers": ["run:errored", "run:needs_attention"],
                    },
                }
            },
        )
        print("  + notification slack-prod")
    else:
        print("  = notification slack-prod (exists)")

    tasks = api.get(f"/api/v1/workspaces/{ws_id}/run-tasks")
    if not any(r["attributes"].get("name") == "compliance-check" for r in tasks.get("data", [])):
        api.post(
            f"/api/v1/workspaces/{ws_id}/run-tasks",
            {
                "data": {
                    "type": "run-tasks",
                    "attributes": {
                        "name": "compliance-check",
                        "url": "https://tasks.example.com/compliance",
                        "stage": "post_plan",
                        "enforcement-level": "advisory",
                    },
                }
            },
        )
        print("  + run task compliance-check")
    else:
        print("  = run task compliance-check (exists)")


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


def seed_inventory(api: Api, ws_id: str):
    """Give the configure workspace an inventory worth photographing.

    The Hosts and Inventory surfaces are the ones that distinguish a configure
    from a plan, and an empty one shows nothing. Modelled as the structures
    Ansible actually has -- hosts, groups, group membership, and variables at
    host, group and `all` level -- rather than as one flat list, because that is
    what the UI renders and what the resolved inventory merges.
    """

    def post(path, typ, attrs=None, rels=None):
        # A link to another resource is a JSON:API RELATIONSHIP here, not an
        # `*-id` attribute -- the house style, and the membership routes refuse
        # an attribute with a 422 that spells out the shape they want.
        data = {"type": typ}
        if attrs:
            data["attributes"] = attrs
        if rels:
            data["relationships"] = rels
        r = api.post(path, {"data": data}, conflict_ok=True)
        return r["data"]["id"] if r else None

    hosts = {
        "web-01.eu-west-1.internal": {"ansible_host": "10.20.1.11", "role": "web"},
        "web-02.eu-west-1.internal": {"ansible_host": "10.20.1.12", "role": "web"},
        "web-03.eu-west-1.internal": {"ansible_host": "10.20.1.13", "role": "web"},
        "cache-01.eu-west-1.internal": {"ansible_host": "10.20.2.21", "role": "cache"},
        "db-01.eu-west-1.internal": {"ansible_host": "10.20.3.31", "role": "db"},
    }
    groups = {"web": ["web-01", "web-02", "web-03"], "cache": ["cache-01"], "db": ["db-01"]}

    # Resolve what is ALREADY there before creating anything. A create that
    # conflicts returns None, and the first version of this took that as "skip
    # the host" -- so on any re-run `host_ids` stayed empty, the membership loop
    # below iterated over nothing, and the seeder reported success having
    # created five hosts in no groups. Idempotent has to mean converges, not
    # "does nothing the second time".
    existing = {
        h["attributes"]["name"]: h["id"]
        for h in api.get(f"/api/v1/workspaces/{ws_id}/inventory/hosts").get("data", [])
    }
    host_ids, made = dict(existing), 0
    for name, hvars in hosts.items():
        if name in host_ids:
            continue
        hid = post(f"/api/v1/workspaces/{ws_id}/inventory/hosts", "inventory-hosts", {"name": name})
        if hid is None:
            continue
        host_ids[name] = hid
        made += 1
        for k, v in hvars.items():
            post(
                f"/api/v1/inventory-hosts/{hid}/vars", "inventory-host-vars", {"key": k, "value": v}
            )

    group_ids = {
        g["attributes"]["name"]: g["id"]
        for g in api.get(f"/api/v1/workspaces/{ws_id}/inventory/groups").get("data", [])
    }
    for g in groups:
        if g in group_ids:
            continue
        gid = post(f"/api/v1/workspaces/{ws_id}/inventory/groups", "inventory-groups", {"name": g})
        if gid:
            group_ids[g] = gid

    for g, prefixes in groups.items():
        gid = group_ids.get(g)
        if not gid:
            continue
        for name, hid in host_ids.items():
            if any(name.startswith(pfx) for pfx in prefixes):
                post(
                    f"/api/v1/inventory-groups/{gid}/hosts",
                    "inventory-host-groups",
                    rels={"host": {"data": {"id": hid, "type": "inventory-hosts"}}},
                )
        post(
            f"/api/v1/inventory-groups/{gid}/vars",
            "inventory-group-vars",
            {"key": "service_tier", "value": g},
        )

    # `group_vars/all`, which is parented on the workspace because `all` cannot
    # be a declared group.
    for k, v in (("ansible_user", "deploy"), ("environment", "prod")):
        post(
            f"/api/v1/workspaces/{ws_id}/inventory/vars",
            "inventory-global-vars",
            {"key": k, "value": v},
        )

    links = sum(
        len(api.get(f"/api/v1/inventory-groups/{gid}/hosts").get("data", []))
        for gid in group_ids.values()
    )
    print(
        f"  · inventory: {len(host_ids)} hosts, {len(group_ids)} groups, {links} membership link(s)"
    )
    if not links:
        print("    WARNING: no host is in a group -- the Inventory tab will look empty")


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
    # Upload to the URL the API HANDS BACK, never to one built from the id.
    #
    # The content endpoint takes no Authorization header -- go-tfe PUTs it
    # without one -- so a bare id would let anyone who learned it overwrite a
    # workspace's state. It therefore carries a signed capability in place of
    # the id (`cap-...` rather than `sv-...`), minted into
    # `hosted-state-upload-url` at create time. This function constructed the
    # path from `sv["data"]["id"]` and had been answering 401 ever since that
    # hardening landed, which is why the screenshots could not be regenerated.
    attrs = sv["data"]["attributes"]
    upload = attrs.get("hosted-state-upload-url")
    if not upload:
        raise SystemExit(
            "the state-version create response carries no hosted-state-upload-url; "
            "the upload contract has moved again and this seeder needs updating"
        )
    api._req(
        "PUT",
        upload,
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
    p.add_argument("--clean", action="store_true", help="delete seeded workspaces, then exit")
    p.add_argument("--no-run", action="store_true", help="seed data but do not execute the plan")
    args = p.parse_args()

    api = Api(args.host, resolve_token(args))

    if args.clean:
        print("Removing seeded workspaces:")
        clean(api)
        return 0

    pool_id = None if args.no_run else pick_pool(api)
    print(f"Seeding the demo estate on {args.host}:")

    # Before the workspaces, and before the --no-run early return below: this
    # is estate furniture rather than run state, and the admin page that shows
    # it is captured whether or not a run is queued.
    seed_vcs_connections(api)
    seed_registry(api)
    seed_autodiscovery(api)

    hero_id = None
    to_run: list[tuple[str, str, str]] = []  # (name, ws_id, variant)
    for name, labels, opts in ESTATE:
        ws_id = create_workspace(api, name, labels, opts, pool_id)
        if name == HERO:
            hero_id = ws_id
        # The configure workspace needs an inventory before its Hosts and
        # Inventory surfaces show anything at all.
        if (opts.get("extra") or {}).get("config-directory") is not None:
            seed_inventory(api, ws_id)
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
        # Only the hero: the notifications and run-tasks tabs are photographed
        # on it, and putting a webhook on every workspace would make the estate
        # read as more configured than a new deployment ever is.
        if name == HERO:
            seed_workspace_integrations(api, ws_id)
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
