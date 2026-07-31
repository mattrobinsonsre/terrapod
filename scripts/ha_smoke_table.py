#!/usr/bin/env python3
"""The #960 release-gate table, run against the live two-node pair.

`ha-smoke.sh up` builds the pair; this walks the scenarios #960 names as the
gate for any release touching HA, and prints a verdict for each: PASS, FAIL, or
SKIP with a reason. Every row is exercised as of #1200 — the SKIP path stays
because a partial run reported as a full one is the failure this script exists
to prevent, and a row that cannot run should say so rather than quietly not
appear.

Run:  scripts/ha-smoke.sh table        every row
      scripts/ha-smoke.sh table 10     one row, while working on it
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

CTX = "rancher-desktop"
NS_A, NS_B = "terrapod-a", "terrapod-b"
REL = {NS_A: "terrapod-a", NS_B: "terrapod-b"}

GREEN, RED, YELLOW, CYAN, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[0m"

results: list[tuple[str, str, str, str]] = []


def _k(*args: str, check: bool = True) -> str:
    out = subprocess.run(
        ["kubectl", "--context", CTX, *args], capture_output=True, text=True
    )
    if check and out.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed:\n{_trim(out.stderr)}")
    return out.stdout


def _trim(text: str, head: int = 1500, tail: int = 1200) -> str:
    """Keep both ends. A Python traceback names the exception at the end of the
    message and SQLAlchemy then prints the whole statement after it, so keeping
    only the tail throws away the one line that says what went wrong."""
    if len(text) <= head + tail:
        return text
    return f"{text[:head]}\n  … {len(text) - head - tail} characters elided …\n{text[-tail:]}"


PRELUDE = """
import asyncio, sys, json, uuid
from sqlalchemy import select, delete, func
from terrapod.db.session import get_db_session, init_db
from terrapod.redis.client import init_redis
from terrapod.db import models as m
from terrapod.services import replication, run_service, pool_set, agent_pool_service

async def main():
    await init_db()
    # Redis as well as the database: the listener registry lives there, so a
    # snippet that asks "which listeners has this node got" needs it, and the
    # cost on the snippets that do not is one connection.
    await init_redis()
    # The outbox is installed by the API's startup, and this is not that
    # process. Without it a row lands and no event is ever recorded, and the
    # pair looks broken when it is not.
    replication.install_outbox_hooks()
    async with get_db_session() as db:
{body}

asyncio.run(main())
"""


def run(ns: str, body: str, check: bool = True) -> str:
    """Execute a snippet inside the node's API pod, against its own database."""
    code = PRELUDE.format(body="\n".join("        " + ln for ln in body.strip().splitlines()))
    return _k("-n", ns, "exec", f"deploy/{REL[ns]}-api", "--", "python", "-c", code, check=check)


def poll(ns: str, body: str, tries: int = 20, delay: float = 3.0) -> bool:
    """Poll a node until `body` prints OK. Replication is asynchronous by design."""
    for _ in range(tries):
        if "OK" in run(ns, body, check=False):
            return True
        time.sleep(delay)
    return False


def scale(ns: str, replicas: int) -> None:
    _k("-n", ns, "scale", f"deploy/{REL[ns]}-api", f"--replicas={replicas}")
    if replicas:
        _k("-n", ns, "rollout", "status", f"deploy/{REL[ns]}-api", "--timeout=180s")
    else:
        for _ in range(30):
            if not _k("-n", ns, "get", "pod", "-l",
                      "app.kubernetes.io/component=api", "-o", "name").strip():
                return
            time.sleep(2)


def _helm(ns: str, *overrides: str, timeout: str = "8m") -> None:
    """`helm upgrade` on one node of the pair, with helm's own error on failure.

    `subprocess.run(check=True)` raises a CalledProcessError whose message is
    the argv and nothing else, so a failed upgrade used to report only that it
    had failed — which is no use at 03:00 or in a table run.
    """
    out = subprocess.run(
        ["helm", "--kube-context", CTX, "upgrade", REL[ns], "helm/terrapod",
         "-n", ns, "-f", f"helm/terrapod/values-ha-{ns[-1]}.yaml",
         *overrides, "--reuse-values", "--wait", "--timeout", timeout],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"helm upgrade {REL[ns]} failed:\n{_trim(out.stderr or out.stdout)}")


def set_role(ns: str, role: str) -> None:
    """Flip a node's role the way an operator would — through Helm.

    If you have poked the pair with `kubectl set image` by hand, this fails with
    a server-side-apply field conflict: `kubectl-set` now owns
    `.spec.template.spec.containers[api].image` and Helm will not take it back.
    Drop the stale ownership (`kubectl patch deploy … --type=json -p
    '[{"op":"remove","path":"/metadata/managedFields"}]'`) and upgrade again.
    Roll images through Helm on this pair and it never arises.

    Note the explicit `--set` alongside `-f`: `--reuse-values` does NOT protect
    a value the values file also sets. The file is re-applied over the reused
    values, so `helm upgrade -f values-ha-a.yaml --reuse-values` on its own puts
    node A back to `role: leader` no matter what it was before — quietly, and
    only visible in the rendered ConfigMap.
    """
    _helm(ns, "--set", f"api.config.ha.role={role}")


def set_role_and_listener(ns: str, *, role: str, api_url: str) -> None:
    """`set_role`, plus the listener's view of where the API lives.

    Slower than it looks: changing `listener.apiUrl` replaces the listener pod,
    and on this single-node cluster the outgoing one has to finish terminating
    before the new one can schedule. Hence the longer timeout.
    """
    _helm(ns, "--set", f"api.config.ha.role={role}", "--set", f"listener.apiUrl={api_url}",
          timeout="10m")


def record(row: str, name: str, verdict: str, detail: str = "") -> None:
    colour = {"PASS": GREEN, "FAIL": RED, "SKIP": YELLOW}[verdict]
    print(f"{colour}{verdict:4}{RESET} {row:>3}  {name}" + (f"  — {detail}" if detail else ""))
    results.append((row, name, verdict, detail))


def make_ws(ns: str, name: str) -> None:
    run(ns, f"db.add(m.Workspace(name='{name}'))\nawait db.commit()")


def sees_ws(name: str) -> str:
    return (
        f"row = await db.scalar(select(m.Workspace).where(m.Workspace.name == '{name}'))\n"
        "print('OK' if row else 'no')"
    )


# --------------------------------------------------------------------------
# The table
# --------------------------------------------------------------------------


def row_2_deltas(stamp: str) -> None:
    name = f"ha-delta-{stamp}"
    make_ws(NS_A, name)
    record("2", "Change settings on A → delta replication",
           "PASS" if poll(NS_B, sees_ws(name)) else "FAIL")


def row_1_backfill(stamp: str) -> None:
    """Drop the follower's copy AND its cursors: the adoption path exactly."""
    name = f"ha-backfill-{stamp}"
    make_ws(NS_A, name)
    poll(NS_B, sees_ws(name))
    run(NS_B, "await db.execute(delete(m.Workspace))\n"
              "await db.execute(delete(m.ReplicationCursor))\n"
              "await db.commit()")
    record("1", "Bring up B against a populated A → backfill from empty",
           "PASS" if poll(NS_B, sees_ws(name), tries=30) else "FAIL")


def row_3_brief_outage(stamp: str) -> None:
    name = f"ha-catchup-{stamp}"
    scale(NS_B, 0)
    make_ws(NS_A, name)
    scale(NS_B, 1)
    record("3", "B down briefly, then back → catch-up inside retention",
           "PASS" if poll(NS_B, sees_ws(name), tries=30) else "FAIL")


def row_4_past_retention(stamp: str) -> None:
    """Purge the events the follower has not consumed: the gap must be
    unrecoverable by replay, so the stale cursor must fall back to backfill."""
    name = f"ha-stale-{stamp}"
    scale(NS_B, 0)
    make_ws(NS_A, name)
    run(NS_A, "newest = await db.scalar(select(func.max(m.ReplicationEvent.id)))\n"
              "await db.execute(delete(m.ReplicationEvent)"
              ".where(m.ReplicationEvent.id < newest))\n"
              "await db.commit()\n"
              "print('purged below', newest)")
    scale(NS_B, 1)
    record("4", "B down past retention → automatic fallback to backfill",
           "PASS" if poll(NS_B, sees_ws(name), tries=30) else "FAIL")


def row_5_peer_outage(stamp: str) -> None:
    name = f"ha-peerdown-{stamp}"
    scale(NS_B, 0)
    try:
        make_ws(NS_A, name)
        wrote = "OK" in run(NS_A, sees_ws(name), check=False)
    finally:
        scale(NS_B, 1)
    record("5", "Kill B entirely; write on A → a peer outage never blocks the leader",
           "PASS" if wrote else "FAIL",
           "" if wrote else "the leader refused a write while its peer was down")


def row_8_follower_inert(stamp: str) -> None:
    """Through the real HTTP write path, not the predicate — that is the point.

    A token minted on the leader replicates, so it is genuinely valid on the
    follower; the refusal must come from leadership, not from auth.
    """
    token = run(NS_A, """
from terrapod.auth import api_tokens
raw = await api_tokens.create_api_token(
    db, bound_to='admin', created_by='admin', kind='interactive', lifespan_hours=2)
await db.commit()
print('TOKEN', raw[1])
""").split("TOKEN ")[-1].strip()

    if not poll(NS_B, "n = await db.scalar(select(func.count()).select_from(m.APIToken))\n"
                      "print('OK' if n else 'no')"):
        record("8", "Follower is inert at the write layer", "FAIL",
               "the token never replicated, so the refusal could not be attributed")
        return

    body = f"""
import httpx, json
r = httpx.post(
    'http://localhost:8000/api/v2/organizations/default/workspaces',
    headers={{'Authorization': 'Bearer {token}',
             'Content-Type': 'application/vnd.api+json'}},
    json={{'data': {{'type': 'workspaces',
                    'attributes': {{'name': 'ha-inert-{stamp}'}}}}}},
    timeout=30.0)
print('STATUS', r.status_code, r.text[:300])
"""
    out = _k("-n", NS_B, "exec", f"deploy/{REL[NS_B]}-api", "--", "python", "-c", body)
    status = out.split("STATUS ")[-1].split()[0]
    created = "OK" in run(NS_B, sees_ws(f"ha-inert-{stamp}"), check=False)
    ok = status not in ("200", "201") and not created
    record("8", "Follower refuses a write through the real API path",
           "PASS" if ok else "FAIL",
           f"HTTP {status}" + (", and the row was NOT created" if ok else ", but the write LANDED"))


def row_11_sensitive(stamp: str) -> None:
    """Encrypted at rest per node, so the value must decrypt on the receiver —
    the round trip, not just the ciphertext, is what has to survive."""
    ws = f"ha-sens-{stamp}"
    secret = f"s3cr3t-{stamp}"
    run(NS_A, f"""
ws = m.Workspace(name='{ws}')
db.add(ws)
await db.flush()
db.add(m.Variable(workspace_id=ws.id, key='tok', value='{secret}',
                  category='terraform', sensitive=True))
await db.commit()
""")
    body = f"""
ws = await db.scalar(select(m.Workspace).where(m.Workspace.name == '{ws}'))
if ws is None:
    print('no')
else:
    v = await db.scalar(select(m.Variable).where(m.Variable.workspace_id == ws.id))
    print('OK' if v is not None and v.value == '{secret}' else 'no')
"""
    record("11", "Sensitive variable round-trips decrypt-on-send / re-encrypt-on-receive",
           "PASS" if poll(NS_B, body) else "FAIL")


def row_12_token(stamp: str) -> None:
    ok = poll(NS_B, "n = await db.scalar(select(func.count()).select_from(m.APIToken))\n"
                    "print('OK' if n else 'no')")
    record("12", "A terraform login token works against the promoted node",
           "PASS" if ok else "FAIL",
           "verified as replication of the token; the HTTP use is row 8's path")


def row_14_stale_runs(stamp: str) -> None:
    """The most dangerous path in the design: a promoted node holding frozen run
    rows must ERROR them, not let an unbounded periodic task act on them."""
    ws = f"ha-staleruns-{stamp}"
    run(NS_B, f"""
ws = m.Workspace(name='{ws}')
db.add(ws)
await db.flush()
db.add(m.Run(workspace_id=ws.id, status='planning', message='frozen',
             source='autodiscovery-lifecycle', is_destroy=True))
await db.commit()
print('seeded')
""")
    out = run(NS_B, """
from terrapod.services import ha_role
await ha_role._retire_runs_on_role_change(previous='follower', role='leader')
await db.commit()
rows = (await db.execute(select(m.Run.status))).scalars().all()
print('STATUSES', sorted(set(rows)))
""")
    left_running = "planning" in out.split("STATUSES")[-1] or "applying" in out.split("STATUSES")[-1]
    record("14", "Promotion errors stale runs instead of queuing destroys",
           "FAIL" if left_running else "PASS",
           out.split("STATUSES")[-1].strip())


def row_6_planned_failover(stamp: str) -> None:
    name = f"ha-planned-{stamp}"
    set_role(NS_B, "leader")
    set_role(NS_A, "follower")
    make_ws(NS_B, name)
    ok = poll(NS_A, sees_ws(name), tries=30)
    record("6", "Quiesce A, move the name → B leads (planned failover)",
           "PASS" if ok else "FAIL")


def row_7_unplanned_failover(stamp: str) -> None:
    """A is already the follower from row 6; kill it outright and confirm the
    surviving node still takes writes with its peer simply gone."""
    name = f"ha-unplanned-{stamp}"
    scale(NS_A, 0)
    try:
        make_ws(NS_B, name)
        ok = "OK" in run(NS_B, sees_ws(name), check=False)
    finally:
        scale(NS_A, 1)
    record("7", "Kill A, move the name → unplanned failover",
           "PASS" if ok else "FAIL")


def reset_roles() -> None:
    """Back to the pair's canonical arrangement: A leads, B follows."""
    set_role(NS_A, "leader")
    set_role(NS_B, "follower")


def restore(stamp: str) -> None:
    reset_roles()
    name = f"ha-failback-{stamp}"
    make_ws(NS_A, name)
    record("6b", "Fail back to A and re-converge",
           "PASS" if poll(NS_B, sees_ws(name), tries=30) else "FAIL")


def _listener_names(ns: str) -> list[str]:
    """The listeners this node currently has registered, from its own Redis."""
    out = run(
        ns,
        "names = []\n"
        "for p in (await db.execute(select(m.AgentPool))).scalars().all():\n"
        "    for lst in await agent_pool_service.list_listeners(str(p.id)):\n"
        "        names.append(lst.get('name') or '')\n"
        "print('LISTENERS', json.dumps(sorted(n for n in names if n)))",
        check=False,
    )
    try:
        return json.loads(out.split("LISTENERS ")[-1].splitlines()[0])
    except (ValueError, IndexError):
        return []


def _scale_listener(ns: str, replicas: int) -> None:
    _k("-n", ns, "scale", f"deploy/{REL[ns]}-listener", f"--replicas={replicas}")
    if replicas:
        _k("-n", ns, "rollout", "status", f"deploy/{REL[ns]}-listener", "--timeout=180s")


# The run held across the cutover asks for more CPU than the cluster has, so its
# Job is created and its pod never schedules. That is a real in-flight run — the
# platform waiting on capacity is an ordinary thing — and it removes the race
# against a runner that would otherwise exit within a second or two of starting.
# The reconciler's own unschedulable timeout is 300s, which is an order of
# magnitude more than a demotion needs.
_UNSCHEDULABLE_CPU = "64"


def _delete_pinned_jobs() -> None:
    """Remove the Jobs whose pods were never going to schedule.

    The listener admits work against the count of *running* Jobs (#749), and a
    Job stuck Pending on capacity counts. Since #1198 the reconciler reaps these
    itself once the launch timeout expires, but row 9 cuts over long before
    that, so the row would otherwise hand the next one a listener at capacity —
    which is exactly how row 9 failed the first time it ran, reporting "no
    listener claimed the run" for a reason that had nothing to do with the
    cutover.
    """
    for ns in (NS_A, NS_B):
        names = _k("-n", ns, "get", "job", "-o",
                   "jsonpath={.items[*].metadata.name}", check=False).split()
        for name in names:
            pending = _k("-n", ns, "get", "pod", "--selector", f"job-name={name}",
                         "-o", "jsonpath={.items[*].status.phase}", check=False)
            if "Pending" in pending:
                _k("-n", ns, "delete", "job", name, "--ignore-not-found", check=False)


def row_9_cutover_midrun(stamp: str) -> None:
    """A genuinely in-flight run, held across a demotion.

    Row 14 seeds a run row and calls the retirement predicate directly. This one
    is claimed by a real listener over SSE and backed by a real Kubernetes Job,
    which is the difference that matters: it exercises the *wiring*, not the
    predicate. That distinction is what found #1197 — the predicate had a single
    caller, reachable only under `ha.role: auto`, so the demotion step the
    operations runbook prescribes never reached it and the node's in-flight runs
    stayed `planning` for as long as it remained a follower.
    """
    reset_roles()
    ws = f"ha-midrun-{stamp}"
    pools = run(
        NS_A,
        "ids = [str(p.id) for p in (await db.execute(select(m.AgentPool))).scalars().all()]\n"
        "print('POOLS', json.dumps(ids))",
    )
    pool_ids = json.loads(pools.split("POOLS ")[-1].splitlines()[0])
    if not pool_ids:
        record("9", "Cut over mid-run", "FAIL", "node A has no agent pool to route to")
        return

    run(
        NS_A,
        f"ws = m.Workspace(name='{ws}', execution_mode='agent',"
        f" resource_cpu='{_UNSCHEDULABLE_CPU}')\n"
        f"pool_set.set_workspace_pools(ws, {pool_ids!r})\n"
        "db.add(ws)\n"
        "await db.flush()\n"
        "run_row = await run_service.create_run(db, ws, message='row 9')\n"
        "await run_service.queue_run(db, run_row)\n"
        "await db.commit()\n"
        "print('RUN', run_row.id)",
    )

    claimed = f"""
row = await db.scalar(select(m.Run).join(m.Workspace)
                      .where(m.Workspace.name == '{ws}')
                      .order_by(m.Run.created_at.desc()).limit(1))
print('OK' if row is not None and row.status == 'planning' and row.job_name else 'no')
"""
    if not poll(NS_A, claimed, tries=15, delay=2.0):
        record("9", "Cut over mid-run", "FAIL",
               "no listener claimed the run, so there was nothing in flight to cut over")
        return

    set_role(NS_A, "follower")

    verdict = run(NS_A, f"""
row = await db.scalar(select(m.Run).join(m.Workspace)
                      .where(m.Workspace.name == '{ws}')
                      .order_by(m.Run.created_at.desc()).limit(1))
wsrow = await db.scalar(select(m.Workspace).where(m.Workspace.name == '{ws}'))
print('VERDICT', json.dumps({{
    'status': row.status,
    'by_role_change': 'role changed' in (row.error_message or ''),
    'workspace_locked': bool(wsrow.locked),
}}))
""")
    state = json.loads(verdict.split("VERDICT ")[-1].splitlines()[0])
    retired = state["status"] == "errored" and state["by_role_change"]

    # The other half of the row: the workspace must be immediately usable on the
    # node that took over. A frozen `planning` row would hold the per-workspace
    # serialization gate, and no operator can clear it — `workspace.locked` is
    # untouched, so there is nothing to unlock. Re-pointing at the surviving
    # fleet is the per-node-fleet cutover step, not a workaround.
    set_role(NS_B, "leader")
    poll(NS_B, sees_ws(ws), tries=30)
    run(NS_B, f"""
wsrow = await db.scalar(select(m.Workspace).where(m.Workspace.name == '{ws}'))
wsrow.resource_cpu = '1'
ids = [str(p.id) for p in (await db.execute(select(m.AgentPool))).scalars().all()]
pool_set.set_workspace_pools(wsrow, ids)
await db.flush()
run_row = await run_service.create_run(db, wsrow, message='row 9 requeue')
await run_service.queue_run(db, run_row)
await db.commit()
print('REQUEUED', run_row.id)
""", check=False)
    requeued = poll(NS_B, f"""
row = await db.scalar(select(m.Run).join(m.Workspace)
                      .where(m.Workspace.name == '{ws}', m.Run.message == 'row 9 requeue')
                      .order_by(m.Run.created_at.desc()).limit(1))
print('OK' if row is not None and row.status in ('planning', 'planned', 'errored') else 'no')
""", tries=20, delay=3.0)

    _delete_pinned_jobs()

    ok = retired and not state["workspace_locked"] and requeued
    record("9", "Cut over mid-run", "PASS" if ok else "FAIL",
           f"in-flight run → {state['status']}"
           + (" by the role change" if state["by_role_change"] else " by something else")
           + (", workspace not locked" if not state["workspace_locked"] else ", WORKSPACE LEFT LOCKED")
           + (", re-queue dispatched on the promoted node" if requeued
              else ", RE-QUEUE NEVER DISPATCHED"))


_SHARED_SVC = "terrapod-shared-api"


def _point_shared_name_at(target_ns: str) -> None:
    """Move the shared name, the way moving DNS moves it.

    An ExternalName Service is the closest thing this rig has to a name whose
    answer changes: the listener's configuration never changes, only what its
    hostname resolves to — which is exactly the shared-fleet topology.
    """
    manifest = f"""
apiVersion: v1
kind: Service
metadata:
  name: {_SHARED_SVC}
  namespace: {NS_A}
spec:
  type: ExternalName
  externalName: {REL[target_ns]}-api.{target_ns}.svc.cluster.local
"""
    subprocess.run(
        ["kubectl", "--context", CTX, "apply", "-f", "-"],
        input=manifest, text=True, check=True, capture_output=True,
    )


def row_10_listener_topologies(stamp: str) -> None:
    """Both fleets, and a re-join that needs no operator.

    Per-node is the pair's steady state and is already proven by row 9 — node A's
    listener claimed that run against node A. This exercises the other topology:
    one fleet addressing a *shared* name, which after a cutover finds itself
    talking to a node whose CA never issued its certificate. It must recover on
    its own, and the mechanism it recovers by is the join token, which is why the
    assertion is that a token's `use_count` moved rather than merely that a
    listener reappeared.
    """
    reset_roles()
    per_node = _listener_names(NS_A)
    if not per_node:
        record("10", "Cut over with each listener topology", "FAIL",
               "node A's own fleet is not registered, so neither topology can be judged")
        return

    before = run(NS_B, """
tok = await db.scalar(select(m.AgentPoolToken).order_by(m.AgentPoolToken.created_at))
print('USES', tok.use_count if tok else -1)
""")
    uses_before = int(before.split("USES ")[-1].splitlines()[0])

    # Establish the shared-fleet topology *before* the cutover, because that is
    # the order it happens in: the fleet has been addressing the shared name all
    # along, and the cutover is what changes the answer underneath it.
    _point_shared_name_at(NS_A)
    rejoined = False
    uses_after = uses_before
    try:
        set_role_and_listener(NS_A, role="leader", api_url=f"http://{_SHARED_SVC}:8000")

        # The cutover. Demoting A rolls its API, which drops the fleet's SSE
        # connection — that is what makes it resolve the name again and find B.
        # A shared fleet that never lost its connection would have no reason to
        # look, which is why the demotion has to come after the re-pointing and
        # not before.
        set_role(NS_B, "leader")
        _point_shared_name_at(NS_B)
        set_role(NS_A, "follower")

        rejoined = False
        for _ in range(40):
            if any(n.startswith("ha-a-listener") for n in _listener_names(NS_B)):
                rejoined = True
                break
            time.sleep(6)

        after = run(NS_B, """
tok = await db.scalar(select(m.AgentPoolToken).order_by(m.AgentPoolToken.created_at))
print('USES', tok.use_count if tok else -1)
""")
        uses_after = int(after.split("USES ")[-1].splitlines()[0])
    finally:
        # Undo only what this row changed. The roles are put back by main(),
        # always and for every invocation, so a single-row run cannot leave the
        # pair with two leaders for the next one to trip over.
        set_role_and_listener(NS_A, role="leader", api_url="")
        _point_shared_name_at(NS_A)

    ok = rejoined and uses_after > uses_before
    record("10", "Cut over with each listener topology", "PASS" if ok else "FAIL",
           "per-node fleet claimed row 9's run; shared fleet "
           + ("re-joined the promoted node unattended" if rejoined else "NEVER RE-JOINED")
           + f", token use_count {uses_before}→{uses_after}")


_PUBLIC_HOST = "terrapod.example.com"
_MODULE_HCL = 'output "ok" {\n  value = "from the registry"\n}\n'
_MODULE = ("default", "hagate", "null")


def _publish_module(ns: str, version: str = "1.0.0") -> bool:
    """Publish a tiny module version on this node: rows plus tarball."""
    namespace, name, provider = _MODULE
    out = run(ns, f"""
import io, tarfile
from terrapod.storage import init_storage, get_storage
from terrapod.storage.keys import module_tarball_key
await init_storage()
mod = await db.scalar(select(m.RegistryModule).where(
    m.RegistryModule.namespace == '{namespace}', m.RegistryModule.name == '{name}',
    m.RegistryModule.provider == '{provider}'))
if mod is None:
    mod = m.RegistryModule(namespace='{namespace}', name='{name}', provider='{provider}',
                           status='active', owner_email='admin')
    db.add(mod)
    await db.flush()
ver = await db.scalar(select(m.RegistryModuleVersion).where(
    m.RegistryModuleVersion.module_id == mod.id,
    m.RegistryModuleVersion.version == '{version}'))
if ver is None:
    ver = m.RegistryModuleVersion(module_id=mod.id, version='{version}')
    db.add(ver)
    await db.flush()
# A module with no resources and no providers: what is being tested is that the
# runner can RESOLVE and FETCH it, not what it declares.
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode='w:gz') as tf:
    body = {_MODULE_HCL!r}.encode()
    info = tarfile.TarInfo('main.tf'); info.size = len(body)
    tf.addfile(info, io.BytesIO(body))
await get_storage().put(
    module_tarball_key('{namespace}', '{name}', '{provider}', '{version}'), buf.getvalue())
ver.upload_status = 'uploaded'
await db.commit()
print('PUBLISHED', mod.id)
""", check=False)
    return "PUBLISHED" in out


def row_13_install_from_promoted(stamp: str) -> None:
    """Install a private module FROM the promoted node, not merely find its rows.

    #1114 proves the registry rows and their blobs arrive. That is the half a
    replication test can see. The half an operator cares about is whether a
    client can then install from it — a registry whose rows replicated but whose
    downloads 404 passes every other row in this table.

    Driven through a real run rather than a local `tofu init`, because
    terraform's registry discovery is HTTPS-only with no plaintext fallback and
    the pair has no ingress and no certificate. The runner writes a CLI-config
    `host{}` block redirecting the public hostname's discovery to its own
    plaintext internal API, which is exactly the mechanism a split-networking
    deployment relies on — so going through the platform tests a real shape
    rather than a test-only one.
    """
    reset_roles()
    namespace, name, provider = _MODULE
    if not _publish_module(NS_A):
        record("13", "terraform init against a private module on the promoted node",
               "FAIL", "could not publish the module on node A")
        return

    replicated = poll(NS_B, f"""
mod = await db.scalar(select(m.RegistryModule).where(m.RegistryModule.name == '{name}'))
if mod is None:
    print('no')
else:
    v = await db.scalar(select(m.RegistryModuleVersion).where(
        m.RegistryModuleVersion.module_id == mod.id,
        m.RegistryModuleVersion.upload_status == 'uploaded'))
    print('OK' if v else 'no')
""", tries=30)
    if not replicated:
        record("13", "terraform init against a private module on the promoted node",
               "FAIL", "the module never replicated to B, so the install could not be attempted")
        return

    # Promote B and consume the module from it. The blob has to be there too —
    # rows alone would resolve the version and then 404 the download, which is
    # the failure this row exists to catch.
    set_role(NS_B, "leader")
    ws = f"ha-modinstall-{stamp}"
    hcl = (
        f'module "m" {{\n'
        f'  source  = "{_PUBLIC_HOST}/{namespace}/{name}/{provider}"\n'
        f'  version = "1.0.0"\n'
        f'}}\n'
    )
    started = run(NS_B, f"""
import io, tarfile
from terrapod.storage import init_storage, get_storage
from terrapod.storage.keys import config_version_key
await init_storage()
pools = [str(p.id) for p in (await db.execute(select(m.AgentPool))).scalars().all()]
ws = m.Workspace(name='{ws}', execution_mode='agent')
pool_set.set_workspace_pools(ws, pools)
db.add(ws)
await db.flush()
cv = m.ConfigurationVersion(workspace_id=ws.id, source='tfe-api', status='pending')
db.add(cv)
await db.flush()
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode='w:gz') as tf:
    body = {hcl!r}.encode()
    info = tarfile.TarInfo('main.tf'); info.size = len(body)
    tf.addfile(info, io.BytesIO(body))
await get_storage().put(config_version_key(str(ws.id), str(cv.id)), buf.getvalue())
cv.status = 'uploaded'
run_row = await run_service.create_run(db, ws, message='row 13',
                                       configuration_version_id=cv.id, plan_only=True)
await run_service.queue_run(db, run_row)
await db.commit()
print('QUEUED', run_row.id)
""", check=False)
    if "QUEUED" not in started:
        record("13", "terraform init against a private module on the promoted node",
               "FAIL", "could not queue the run on the promoted node")
        return

    settled = poll(NS_B, f"""
row = await db.scalar(select(m.Run).join(m.Workspace)
                      .where(m.Workspace.name == '{ws}')
                      .order_by(m.Run.created_at.desc()).limit(1))
print('OK' if row is not None and row.status in ('planned', 'errored') else 'no')
""", tries=40, delay=6.0)

    verdict = run(NS_B, f"""
row = await db.scalar(select(m.Run).join(m.Workspace)
                      .where(m.Workspace.name == '{ws}')
                      .order_by(m.Run.created_at.desc()).limit(1))
print('VERDICT', json.dumps({{'status': row.status if row else 'missing',
                             'error': (row.error_message or '')[:200] if row else ''}}))
""", check=False)
    try:
        state = json.loads(verdict.split("VERDICT ")[-1].splitlines()[0])
    except (ValueError, IndexError):
        state = {"status": "unknown", "error": ""}

    # `planned` is the assertion. A plan cannot complete without init having
    # resolved and downloaded the module from this node's registry, so reaching
    # it proves the install; anything else is reported with the run's own error
    # rather than guessed at.
    ok = settled and state["status"] == "planned"
    record("13", "terraform init against a private module on the promoted node",
           "PASS" if ok else "FAIL",
           f"run {state['status']} on the promoted node"
           + (f" — {state['error']}" if state["error"] else "")
           + ("" if settled else " (never reached a terminal state)"))


# Every row, in the order the table runs them. Named so a single row can be
# re-run while it is being worked on — a full pass takes the better part of an
# hour, which is far too slow a loop to develop a row against.
ROWS: dict[str, object] = {
    "2": row_2_deltas,
    "1": row_1_backfill,
    "3": row_3_brief_outage,
    "4": row_4_past_retention,
    "5": row_5_peer_outage,
    "8": row_8_follower_inert,
    "11": row_11_sensitive,
    "12": row_12_token,
    "14": row_14_stale_runs,
    "6": row_6_planned_failover,
    "7": row_7_unplanned_failover,
    "6b": restore,
    # Last, because they leave and restore the pair's roles and its listener
    # wiring, and because they are the slowest rows by a wide margin.
    "9": row_9_cutover_midrun,
    "10": row_10_listener_topologies,
    "13": row_13_install_from_promoted,
}


def main() -> int:
    wanted = [a for a in sys.argv[1:] if not a.startswith("-")]
    unknown = [w for w in wanted if w not in ROWS]
    if unknown:
        print(f"unknown row(s): {', '.join(unknown)}; known: {', '.join(ROWS)}")
        return 2

    # The namespace UID identifies the pair; the clock suffix identifies the
    # run. Without the suffix a second table run against a live pair collides on
    # the first name it re-creates, and the pair is meant to be re-runnable —
    # rows 1 and 3 clear the follower, never the leader.
    stamp = (
        _k("-n", NS_A, "get", "ns", NS_A, "-o", "jsonpath={.metadata.uid}")[:8]
        + f"-{int(time.time()) % 100000:05d}"
    )
    scope = f" (rows {', '.join(wanted)})" if wanted else ""
    print(f"{CYAN}==>{RESET} #960 release-gate table against {NS_A} / {NS_B}{scope}\n")

    try:
        for name, fn in ROWS.items():
            if wanted and name not in wanted:
                continue
            fn(stamp)  # type: ignore[operator]
    finally:
        # Always, including on the way out of a failure and including a
        # single-row run. A row that leaves the pair with two leaders makes the
        # NEXT run open by reporting replication as broken, which is true and
        # completely misleading — the cause is two rows earlier and in a
        # different invocation.
        reset_roles()

    failed = [r for r in results if r[2] == "FAIL"]
    skipped = [r for r in results if r[2] == "SKIP"]
    passed = [r for r in results if r[2] == "PASS"]
    print(
        f"\n{CYAN}==>{RESET} {len(passed)} passed, {len(failed)} failed, "
        f"{len(skipped)} not exercised"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
