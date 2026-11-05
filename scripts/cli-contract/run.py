#!/usr/bin/env python3
"""Drive the real `tofu` binary against a running Terrapod (#2341).

See README.md beside this file for why it exists, why OpenTofu only, and why
it needs real TLS.

Standard library plus the `tofu` binary — it runs on a CI runner with nothing
Terrapod-specific installed.

    python3 scripts/cli-contract/run.py --host terrapod.local \
        --email admin --password admin
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Every check this suite makes, each naming the catalogue section it covers so
#: `check_cli_surface_covered.py` can tie the two together. A row added to
#: docs/tfe-cli-surface.md with no check here is the thing that gate reports.
CHECKS: list[tuple[str, str]] = []


class Fail(Exception):
    """A contract failure. Carries the section so the report names it."""


def check(section: str, name: str, needs: tuple[str, ...] = ()):
    """Register a check.

    `needs` names checks that must run first. The lifecycle checks build the
    working directory and the applied state that the state checks read, so
    `--only` has to pull its prerequisites in -- without that it reports a
    FileNotFoundError, which looks like a product failure and is not. That is
    exactly what the first attempt at the state-upload mutation proof reported.
    """

    def wrap(fn):
        CHECKS.append((section, name))
        fn._section = section
        fn._name = name
        fn._needs = needs
        return fn

    return wrap


class Ctx:
    """What every check is handed: where the instance is, and how to reach it."""

    def __init__(self, base: str, token: str, insecure: bool) -> None:
        self.base = base.rstrip("/")
        self.host = self.base.split("://", 1)[1]
        self.token = token
        self.insecure = insecure
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="tp-cli-contract-"))
        # A credentials file of our own. NEVER the operator's
        # ~/.terraform.d/credentials.tfrc.json -- this runs on a developer's
        # machine as readily as on a runner.
        self.creds = self.work / "creds.tfrc"
        self.creds.write_text(f'credentials "{self.host}" {{\n  token = "{token}"\n}}\n')
        self.creds.chmod(0o600)

    @property
    def tls(self) -> ssl.SSLContext:
        c = ssl.create_default_context()
        if self.insecure:
            c.check_hostname = False
            c.verify_mode = ssl.CERT_NONE
        return c

    def api(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/vnd.api+json")
        try:
            with urllib.request.urlopen(req, context=self.tls, timeout=60) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            return e.code, e.read()[:400].decode(errors="replace")

    def tofu(
        self,
        *args: str,
        cwd: pathlib.Path,
        expect_ok: bool = True,
        # 240s, not the 900s this started with. The whole suite finishes in about
        # two minutes against a live stack, so a single call past four minutes is
        # stuck rather than slow -- and at 900s one such call ate 15 of the CI
        # job's 25-minute budget, so the job was CANCELLED and the suite never
        # got to report anything at all. A timeout that outlives its caller's
        # budget cannot fail; it can only hide.
        timeout: int = 240,
    ) -> subprocess.CompletedProcess:
        """Run tofu with OUR credentials file and no inherited TF_* settings.

        `TF_CLI_CONFIG_FILE` rather than writing to the home directory, and the
        inherited TF_* environment is stripped: a developer running this by
        hand may have TF_LOG, a TF_WORKSPACE or a cloud block override set, and
        a suite whose result depends on the operator's shell is not a contract
        test.
        """
        env = {k: v for k, v in os.environ.items() if not k.startswith("TF_")}
        env["TF_CLI_CONFIG_FILE"] = str(self.creds)
        env["TF_IN_AUTOMATION"] = "1"
        env["CHECKPOINT_DISABLE"] = "1"
        # The command is `tofu` resolved from PATH by shutil.which and the
        # arguments are literals from the checks above; the env is this
        # process's own with TF_* stripped and four keys set. Nothing here
        # comes from a request, a file or a remote. The rule is right in
        # general -- a tainted argv or env reaching a subprocess is how a
        # command injection lands -- and this is a CI harness whose whole
        # job is to invoke a known binary.
        p = subprocess.run(
            [TOFU, *args, "-no-color"] if args[0] != "version" else [TOFU, *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            # The structural half of the no-prompt rule. `capture_output` makes
            # stdout a pipe but leaves stdin INHERITED, so a tofu that decides to
            # prompt blocks on a read that never returns -- no network traffic,
            # no output, nothing to see. That is consistent with the first CI
            # hang: 28 connections through the forward, then fourteen minutes of
            # silence. `-input=false` on each call states the intent; this makes
            # a prompt impossible whether or not someone remembers the flag.
            stdin=subprocess.DEVNULL,
            text=True,
            errors="replace",  # tofu writes non-UTF-8 bytes; never crash on output
            timeout=timeout,
            check=False,  # the caller decides; some checks assert a REFUSAL
        )
        if expect_ok and p.returncode != 0:
            raise Fail(f"`tofu {' '.join(args)}` exited {p.returncode}\n{p.stdout}\n{p.stderr}")
        return p

    def workspace(self, name: str, directory: pathlib.Path, extra: str = "") -> None:
        """Write a cloud-block configuration pointing at this instance."""
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "main.tf").write_text(
            f"terraform {{\n"
            f"  cloud {{\n"
            f'    hostname     = "{self.host}"\n'
            f'    organization = "default"\n'
            f'    workspaces {{ name = "{name}" }}\n'
            f"  }}\n"
            f"}}\n"
            f"{extra}"
        )


TOFU = shutil.which("tofu") or ""


# ── Discovery / Auth ────────────────────────────────────────────────────


@check("Discovery / Auth", "the discovery document advertises the CLI services")
def discovery(ctx: Ctx) -> None:
    code, doc = ctx.api("GET", "/.well-known/terraform.json")
    if code != 200:
        raise Fail(f"/.well-known/terraform.json answered {code}")
    # A missing service does not error -- the CLI simply behaves as if the
    # feature is absent, which is why this is checked at all. The comment here
    # used to say "these three" above a loop over TWO, so the two registry
    # protocols were advertised as covered and were not: dropping `modules.v1`
    # would make every `registry.<host>/...` module source resolve nowhere,
    # silently. Verified against a live document, which carries exactly these
    # four plus `tfe.v2.1`, `tfe.v2.2` and `terrapod-version`.
    for key in ("tfe.v2", "login.v1", "modules.v1", "providers.v1"):
        if key not in doc:
            raise Fail(f"the discovery document carries no {key!r}: {sorted(doc)}")
    login = doc["login.v1"]
    for field in ("client", "grant_types", "authz", "token"):
        if field not in login:
            raise Fail(f"login.v1 carries no {field!r}: {sorted(login)}")


@check("Discovery / Auth", "the organization run queue and capacity answer")
def run_queue_and_capacity(ctx: Ctx) -> None:
    """Both were catalogued as in-scope and neither was served (#2378).

    A 404 from either is FATAL upstream, not cosmetic: `waitForRun` does
    `return r, generalError("Failed to retrieve queue", err)`, so the plan or
    apply aborts. And it is reachable -- `confirmed` is a real `tfe.RunStatus`,
    reported verbatim, and `waitForRun` returns early only on a status that is
    neither `pending` nor `confirmed`, so an apply not claimed within
    `backoffMin` = 1.000s walks straight into it.

    Asserted through the API rather than by driving `tofu`, deliberately. The
    CLI reaches the queue block only while a run is still `confirmed`, which on
    a responsive stack lasts a few hundred milliseconds -- a check that raced
    that would fail for timing reasons and teach everyone to re-run it. What is
    deterministic, and what actually broke, is that the two endpoints exist and
    carry the shapes `go-tfe` unmarshals.
    """
    code, body = ctx.api("GET", "/api/tfe/v2/organizations/default/runs/queue")
    if code != 200:
        raise Fail(f"the run queue answered {code}: {body}")
    if not isinstance(body.get("data"), list):
        raise Fail(f"the run queue is not a collection: {sorted(body)}")
    # `rq.CurrentPage >= rq.TotalPages` is what ends go-tfe's paging loop, and
    # it sends no page params on the first call -- so one page is what keeps the
    # loop terminating without a `next-page` key Terrapod does not emit.
    page = (body.get("meta") or {}).get("pagination") or {}
    if page.get("total-pages") not in (0, 1):
        raise Fail(f"the unpaged queue reports total-pages={page.get('total-pages')!r}")
    for item in body["data"]:
        attrs = item.get("attributes") or {}
        if not isinstance(attrs.get("position-in-queue"), int):
            raise Fail(
                "a queue item carries no integer `position-in-queue`; without it "
                "`waitForRun` leaves position at 0, never calls ReadCapacity, and "
                f"prints nothing about the queue: {sorted(attrs)}"
            )

    code, body = ctx.api("GET", "/api/tfe/v2/organizations/default/capacity")
    if code != 200:
        raise Fail(f"capacity answered {code}: {body}")
    data = body.get("data") or {}
    if data.get("type") != "organization-capacity":
        raise Fail(f"capacity type is {data.get('type')!r}, which go-tfe will not match")
    attrs = data.get("attributes") or {}
    for field in ("pending", "running"):
        if not isinstance(attrs.get(field), int):
            raise Fail(f"capacity `{field}` is {attrs.get(field)!r}, and tfe.Capacity wants an int")


# ── The cloud-block lifecycle ───────────────────────────────────────────
#
# `terraform_data` throughout, deliberately. It is a BUILT-IN managed resource,
# so init downloads no provider and the suite needs no registry access at all --
# which keeps it testing the contract rather than provider plumbing, and lets it
# run in a sealed runner. Measured: init and plan both succeed offline.

#: The checks that build the working directory and the applied state every
#: state check reads. Named rather than inferred, so `--only` pulls them in.
_LIFECYCLE = (
    "init against a cloud block finds the workspace",
    "plan and apply run, and the state lands server-side",
)

FIXTURE = """
resource "terraform_data" "probe" {
  input = var.value
}

variable "value" {
  type    = string
  default = "contract"
}

output "probe" {
  value = terraform_data.probe.output
}

# Sensitive on purpose. `tofu output` fetches a sensitive value through a
# SECOND route -- the per-output one -- so without this the suite would only
# ever exercise the collection, and a reveal route that 403'd would pass.
# Derived from the VARIABLE, not from the resource: the `import` check drives
# this same configuration in a directory with no applied state, where
# `terraform_data.probe.output` is null and interpolating it into a string is a
# hard error. A variable always has its default.
output "probe_secret" {
  value     = "secret-${var.value}"
  sensitive = true
}
"""


def _make_workspace(ctx: Ctx, name: str) -> str:
    code, body = ctx.api(
        "POST",
        "/api/v1/workspaces",
        {"data": {"type": "workspaces", "attributes": {"name": name, "execution-mode": "local"}}},
    )
    if code not in (200, 201):
        raise Fail(f"could not create workspace {name}: {code} {body}")
    return body["data"]["id"]


@check("Workspaces", "init against a cloud block finds the workspace")
def cloud_init(ctx: Ctx) -> None:
    d = ctx.work / "lifecycle"
    ctx.workspace(ctx.ws_name, d, FIXTURE)
    out = ctx.tofu("init", "-input=false", cwd=d).stdout
    if "Initializing" not in out:
        raise Fail(f"init produced no recognisable output:\n{out}")


@check(
    "Runs / Plans / Applies",
    "plan and apply run, and the state lands server-side",
    needs=("init against a cloud block finds the workspace",),
)
def cloud_apply(ctx: Ctx) -> None:
    d = ctx.work / "lifecycle"
    ctx.tofu("plan", "-input=false", cwd=d)
    ctx.tofu("apply", "-auto-approve", "-input=false", cwd=d)
    code, body = ctx.api("GET", f"/api/tfe/v2/workspaces/{ctx.ws_id}/current-state-version")
    if code != 200:
        raise Fail(f"after apply, current-state-version answered {code} {body}")
    serial = body["data"]["attributes"].get("serial")
    if serial is None:
        raise Fail(f"the state version carries no serial: {body['data']['attributes']}")


@check("Runs / Plans / Applies", "`plan -out` reaches the server as a saved-plan run")
def saved_plan(ctx: Ctx) -> None:
    """#1903: `save-plan` was read off the run-create body by nobody.

    The route was listed, implemented and exercised, and `tofu plan -out`
    silently produced an ordinary run -- which no route-shape snapshot can see.

    AGENT mode, necessarily. In local mode `tofu` plans on the machine it is
    run from and pushes the state afterwards, so there is no server-side run to
    carry the attribute; the first version of this check asserted against a
    local-mode workspace and reported "created no run at all", which was true
    and meant nothing.

    The run only has to be CREATED, not to finish -- the attribute is set when
    the CLI posts it. So the plan is given a short deadline and the run list is
    read afterwards; a server-side plan takes as long as it takes and waiting
    for one would make this the slowest check here by an order of magnitude.
    """
    pools = ctx.api("GET", "/api/v1/agent-pools")[1]
    if not pools.get("data"):
        raise Fail("no agent pool exists, so agent-mode execution cannot be exercised")
    pool = pools["data"][0]["id"]

    code, body = ctx.api(
        "POST",
        "/api/v1/workspaces",
        {
            "data": {
                "type": "workspaces",
                "attributes": {
                    "name": ctx.agent_ws_name,
                    "execution-mode": "agent",
                    "agent-pool-ids": [pool],
                },
            }
        },
    )
    if code not in (200, 201):
        raise Fail(f"could not create an agent-mode workspace: {code} {body}")
    ws = body["data"]["id"]

    d = ctx.work / "savedplan"
    ctx.workspace(ctx.agent_ws_name, d, FIXTURE)
    ctx.tofu("init", "-input=false", cwd=d)
    try:
        ctx.tofu("plan", "-input=false", "-out=saved.tfplan", cwd=d, timeout=120, expect_ok=False)
    except (subprocess.TimeoutExpired, UnicodeDecodeError):
        # The run exists by now; finishing it is not what this asserts.
        #
        # UnicodeDecodeError as well as the timeout: building TimeoutExpired
        # decodes whatever stdout had buffered, and a `plan -out` emits bytes
        # that are not UTF-8 -- so the decode error propagates INSTEAD of the
        # timeout and the check reports a crash rather than a verdict.
        pass

    code, body = ctx.api("GET", f"/api/v1/workspaces/{ws}/runs?page%5Bsize%5D=10")
    if code != 200:
        raise Fail(f"run list answered {code}")
    runs = body.get("data", [])
    if not runs:
        raise Fail("`tofu plan -out` against an agent-mode workspace created no run")
    if not any(r["attributes"].get("save-plan") for r in runs):
        got = [{k: v for k, v in r["attributes"].items() if "plan" in k} for r in runs[:3]]
        raise Fail(
            "no run carries save-plan=true, so `tofu plan -out` produced an "
            f"ordinary run -- the #1903 regression. Recent runs: {got}"
        )


# ── State ───────────────────────────────────────────────────────────────


@check("State Versions", "state pull returns what apply wrote", needs=_LIFECYCLE)
def state_pull(ctx: Ctx) -> None:
    d = ctx.work / "lifecycle"
    raw = ctx.tofu("state", "pull", cwd=d).stdout
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        raise Fail(f"state pull did not return JSON: {e}\n{raw[:300]}") from e
    if not doc.get("resources"):
        raise Fail(f"the pulled state declares no resources: {sorted(doc)}")


@check("State Versions", "state list names the applied resource", needs=_LIFECYCLE)
def state_list(ctx: Ctx) -> None:
    d = ctx.work / "lifecycle"
    out = ctx.tofu("state", "list", cwd=d).stdout
    if "terraform_data.probe" not in out:
        raise Fail(f"state list does not name the applied resource:\n{out}")


@check("State Versions", "state push uploads through the capability URL", needs=_LIFECYCLE)
def state_push(ctx: Ctx) -> None:
    """The upload PUT carries NO Authorization header.

    go-tfe pushes it without one, so the id in the path was replaced by a
    signed capability. `seed.py` kept building the path from the raw id and
    answered 401 for months with nothing to notice -- this is the check that
    would have caught it.
    """
    d = ctx.work / "lifecycle"
    raw = ctx.tofu("state", "pull", cwd=d).stdout
    doc = json.loads(raw)
    before = doc["serial"]
    doc["serial"] = before + 1
    marker = f"pushed-by-cli-contract-{os.getpid()}"
    doc.setdefault("outputs", {})["cli_contract"] = {"value": marker, "type": "string"}
    pushed = ctx.work / "pushed.tfstate"
    pushed.write_text(json.dumps(doc))
    ctx.tofu("state", "push", str(pushed), cwd=d)

    # The serial ADVANCED and the CONTENT is what was pushed. Not an exact
    # serial: tofu manages that number itself and bumps it on push, so pinning
    # the arithmetic asserts tofu's behaviour rather than Terrapod's. The
    # marker is what proves the bytes arrived -- which is the thing the
    # capability URL either carries or does not.
    code, body = ctx.api("GET", f"/api/tfe/v2/workspaces/{ctx.ws_id}/current-state-version")
    if code != 200:
        raise Fail(f"current-state-version answered {code} after push")
    now = body["data"]["attributes"]["serial"]
    if now <= before:
        raise Fail(f"state push did not land: serial is still {now}, was {before}")
    back = json.loads(ctx.tofu("state", "pull", cwd=d).stdout)
    got = (back.get("outputs") or {}).get("cli_contract", {}).get("value")
    if got != marker:
        raise Fail(f"the pushed state did not round-trip: output is {got!r}, pushed {marker!r}")


@check("State Versions", "output reads the state-version outputs", needs=_LIFECYCLE)
def state_version_outputs(ctx: Ctx) -> None:
    """`tofu output` was exit 1 against every cloud-block workspace (#2376).

        Error: could not read state version outputs: resource not found

    `GetRootOutputValues` has no fallback on error, so the command failed
    outright rather than degrading to the full state -- and nothing noticed,
    because every other check here reads outputs out of the state document
    instead of asking the CLI for them.

    Two routes, driven in one call: the CLI reads the collection, then fetches
    each output it was told is SENSITIVE through the per-output route. So a
    reveal route that refused would fail this, where an API test asserting the
    collection alone would not.
    """
    d = ctx.work / "lifecycle"
    raw = ctx.tofu("output", "-json", cwd=d).stdout
    try:
        outs = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Fail(f"output -json did not produce JSON ({exc}):\n{raw}") from exc

    probe = outs.get("probe")
    if not isinstance(probe, dict):
        raise Fail(f"output -json carries no `probe`: {sorted(outs)}")
    if probe.get("value") != "contract":
        raise Fail(f"`probe` is {probe.get('value')!r}, expected 'contract'")
    # The cty type, which is what `detailed-type` carries. A nil on ANY one
    # output makes the CLI abandon the whole path and fall back to the full
    # state, so getting it wrong looks like success here unless it is asserted.
    if probe.get("type") != "string":
        raise Fail(f"`probe` reports type {probe.get('type')!r}, expected 'string'")

    secret = outs.get("probe_secret")
    if not isinstance(secret, dict):
        raise Fail(f"output -json carries no `probe_secret`: {sorted(outs)}")
    if not secret.get("sensitive"):
        raise Fail("`probe_secret` did not come back marked sensitive")
    if secret.get("value") != "secret-contract":
        raise Fail(
            f"`probe_secret` is {secret.get('value')!r}, expected 'secret-contract' -- "
            "the per-output reveal route did not serve it"
        )

    # And the other half of the split, asserted at the API because the CLI
    # cannot show it: the COLLECTION withholds a sensitive value. If it served
    # one, a read-tier principal would get it from the route that is gated at
    # the read tier rather than at the plan tier.
    code, body = ctx.api("GET", f"/api/tfe/v2/workspaces/{ctx.ws_id}/current-state-version-outputs")
    if code != 200:
        raise Fail(f"current-state-version-outputs answered {code}: {body}")
    by_name = {d["attributes"]["name"]: d["attributes"] for d in body["data"]}
    if "probe_secret" not in by_name:
        raise Fail(f"the collection omits `probe_secret` entirely: {sorted(by_name)}")
    if "value" in by_name["probe_secret"]:
        raise Fail("the collection served a sensitive value; it must omit it")
    if by_name.get("probe", {}).get("value") != "contract":
        raise Fail(f"the collection withheld a NON-sensitive value: {by_name.get('probe')}")


@check("Workspaces", "lock, unlock and force-unlock", needs=_LIFECYCLE)
def locking(ctx: Ctx) -> None:
    """docs/runbooks.md tells operators to force-unlock. Nothing verified it."""
    code, _ = ctx.api(
        "POST", f"/api/tfe/v2/workspaces/{ctx.ws_id}/actions/lock", {"reason": "cli-contract"}
    )
    if code not in (200, 409):
        raise Fail(f"lock answered {code}")
    code, body = ctx.api("GET", f"/api/tfe/v2/workspaces/{ctx.ws_id}")
    if not body["data"]["attributes"].get("locked"):
        raise Fail("the workspace does not report itself locked after lock")
    d = ctx.work / "lifecycle"
    # A locked workspace must refuse a plan -- that is what the lock is FOR.
    p = ctx.tofu("plan", "-input=false", cwd=d, expect_ok=False)
    if p.returncode == 0:
        raise Fail("a plan succeeded against a locked workspace")
    ctx.tofu("force-unlock", "-force", ctx.ws_id, cwd=d, expect_ok=False)
    code, body = ctx.api("GET", f"/api/tfe/v2/workspaces/{ctx.ws_id}")
    if body["data"]["attributes"].get("locked"):
        ctx.api("POST", f"/api/tfe/v2/workspaces/{ctx.ws_id}/actions/unlock", {})
        code, body = ctx.api("GET", f"/api/tfe/v2/workspaces/{ctx.ws_id}")
        if body["data"]["attributes"].get("locked"):
            raise Fail("the workspace is still locked after force-unlock and unlock")


@check("Runs / Plans / Applies", "import adopts an existing object into state")
def cli_import(ctx: Ctx) -> None:
    """`import` has no automated coverage anywhere else."""
    d = ctx.work / "import"
    ctx.workspace(ctx.import_ws_name, d, FIXTURE)
    ctx.tofu("init", "-input=false", cwd=d)
    p = ctx.tofu(
        "import", "-input=false", "terraform_data.probe", "imported-id", cwd=d, expect_ok=False
    )
    if p.returncode != 0:
        raise Fail(f"import exited {p.returncode}\n{p.stdout}\n{p.stderr}")
    out = ctx.tofu("state", "list", cwd=d).stdout
    if "terraform_data.probe" not in out:
        raise Fail(f"after import the resource is not in state:\n{out}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", required=True, help="hostname of the Terrapod instance")
    ap.add_argument("--email", default="admin")
    ap.add_argument("--password", default="admin")
    ap.add_argument(
        "--insecure", action="store_true", help="skip TLS verification for the API probes only"
    )
    ap.add_argument("--ca", help="PEM bundle to trust, for a stack whose CA python does not carry")
    ap.add_argument("--only", help="comma-separated check names to run")
    args = ap.parse_args()

    # Line-buffered, and done HERE rather than left to a `python3 -u` in the
    # caller, so it holds however the suite is invoked. Python block-buffers
    # stdout when it is not a tty, so on a CI runner nothing is flushed until
    # exit -- and a run that is CANCELLED never exits. The first hang in CI
    # therefore reported absolutely nothing: 22 minutes, no output, no clue
    # which check was stuck. The tell, visible in the run before it, was a
    # stderr FATAL appearing ABOVE the stdout banner that is printed first.
    sys.stdout.reconfigure(line_buffering=True)

    if not TOFU:
        print("FATAL: `tofu` is not on PATH. This suite drives the real binary.", file=sys.stderr)
        return 2

    base = f"https://{args.host}"
    # The third and last subprocess call in this file, and suppressed for the
    # same reason as the two above: `TOFU` is `shutil.which("tofu")`, so semgrep
    # taints it as PATH-derived -- which is exactly what this suite is for. It
    # drives the real binary an operator has installed; resolving it from PATH
    # is the feature, not a lapse. The argv is a literal.
    ver = subprocess.run([TOFU, "version"], capture_output=True, text=True, check=False)
    # `version` on a broken binary can print nothing at all, and an IndexError
    # here would report as a suite crash rather than as "tofu does not run".
    first = ver.stdout.splitlines()
    print(f"  tofu:   {first[0] if first else f'(no version output, rc={ver.returncode})'}")
    print(f"  target: {base}")

    # scripts/mint-token.py, as a subprocess through its documented interface
    # -- it is a CLI that prints the raw token on stdout and nothing else, not
    # an importable module. Reused rather than reimplemented so a break in the
    # PKCE login path surfaces here instead of being masked by a shortcut.
    # sys.executable running this repository's own scripts/mint-token.py,
    # with arguments this function built. SSL_CERT_FILE is the only env key
    # added, from an explicit --ca flag.
    #
    # The directive sits immediately above the ARGV LIST rather than above the
    # call, because that is the line semgrep anchors the finding to -- and a
    # directive one line too high registers no suppression at all while reading
    # exactly like one that does. Measured with the gate's own pinned image:
    # this is the only one of the three subprocess calls in this file that the
    # rule matches, so the other two carry the reasoning and no directive.
    mint = subprocess.run(
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
        [
            sys.executable,
            str(ROOT / "scripts" / "mint-token.py"),
            "--url",
            base,
            "--email",
            args.email,
            "--password",
            args.password,
        ],
        capture_output=True,
        text=True,
        check=False,
        # mint-token.py verifies TLS, which is correct. Against a local stack
        # whose CA python does not carry, point it at the CA rather than
        # teaching it to skip verification.
        env={**os.environ, **({"SSL_CERT_FILE": args.ca} if args.ca else {})},
    )
    if mint.returncode != 0:
        print(
            f"FATAL: could not mint a token (the OAuth2 + PKCE flow itself failed)\n{mint.stderr}",
            file=sys.stderr,
        )
        return 2
    token = mint.stdout.strip()

    ctx = Ctx(base, token, args.insecure)
    suffix = os.environ.get("GITHUB_RUN_ID", str(os.getpid()))
    ctx.ws_name = f"cli-contract-{suffix}"
    ctx.import_ws_name = f"cli-contract-import-{suffix}"
    ctx.agent_ws_name = f"cli-contract-agent-{suffix}"
    ctx.ws_id = _make_workspace(ctx, ctx.ws_name)
    _make_workspace(ctx, ctx.import_ws_name)
    print(f"  workspace: {ctx.ws_name} ({ctx.ws_id})")
    print(f"  scratch:   {ctx.work}\n")

    wanted = set(args.only.split(",")) if args.only else None
    if wanted:
        by_name = {f._name: f for f in globals().values() if callable(f) and hasattr(f, "_name")}
        unknown = wanted - set(by_name)
        if unknown:
            print(f"FATAL: no such check(s): {sorted(unknown)}", file=sys.stderr)
            return 2
        # Transitive, so naming a state check pulls the whole lifecycle in.
        pending = list(wanted)
        while pending:
            for dep in getattr(by_name[pending.pop()], "_needs", ()):
                if dep not in wanted:
                    wanted.add(dep)
                    pending.append(dep)
        print(f"  --only resolved to {len(wanted)} check(s) including prerequisites\n")
    fns = [v for v in globals().values() if callable(v) and hasattr(v, "_section")]
    # Registration order IS execution order: the lifecycle has to apply before
    # anything can pull its state.
    order = {name: i for i, (_s, name) in enumerate(CHECKS)}
    fns.sort(key=lambda f: order[f._name])

    failures: list[tuple[str, str, str]] = []
    ran = 0
    # A wall-clock budget, because being CANCELLED is strictly worse than
    # failing: the job's timeout kills the process, so nothing is reported at
    # all and the next person has a red check and no verdict. That is exactly
    # what happened on the first CI hang. The per-call timeout cannot prevent
    # it -- 15 tofu calls at 240s each outlive any sane job budget -- so the
    # suite bounds ITSELF and always gets to print.
    #
    # 12 minutes is absurdly generous and deliberately so: measured against a
    # live stack the nine checks total ELEVEN SECONDS, slowest 4.2s. Anything
    # approaching this budget is stuck, not slow.
    budget = 720.0
    started = time.monotonic()
    skipped = 0
    for fn in fns:
        if wanted and fn._name not in wanted:
            continue
        spent = time.monotonic() - started
        if spent > budget:
            skipped += 1
            print(f"  BUDGET [{fn._section}] {fn._name}  (not run -- {spent:.0f}s elapsed)")
            failures.append(
                (
                    fn._section,
                    fn._name,
                    f"not run: the suite passed its {budget:.0f}s budget after "
                    f"{ran} check(s). Something before this hung. The suite stops "
                    f"itself rather than letting the job time out with no verdict.",
                )
            )
            continue
        ran += 1
        # The name goes out BEFORE the check runs, and is flushed, so a hang
        # names the check it is hanging in. Printing only the verdict afterwards
        # is what made the first CI hang undiagnosable.
        print(f"  .... [{fn._section}] {fn._name}")
        t0 = time.monotonic()
        try:
            fn(ctx)
            print(f"  PASS  [{fn._section}] {fn._name}  ({time.monotonic() - t0:.1f}s)")
        except Fail as e:
            print(f"  FAIL  [{fn._section}] {fn._name}  ({time.monotonic() - t0:.1f}s)")
            failures.append((fn._section, fn._name, str(e)))
        except Exception as e:
            print(f"  ERROR [{fn._section}] {fn._name}  ({time.monotonic() - t0:.1f}s)")
            failures.append((fn._section, fn._name, f"{type(e).__name__}: {e}"))

    # Best effort cleanup. A leftover workspace on a dev stack is untidy;
    # failing the suite over it would report the wrong thing.
    code, body = ctx.api("GET", "/api/v1/workspaces?page%5Bsize%5D=0")
    if code == 200:
        for w in body.get("data", []):
            if w["attributes"]["name"] in (
                ctx.ws_name,
                ctx.import_ws_name,
                ctx.agent_ws_name,
            ):
                ctx.api("DELETE", f"/api/v1/workspaces/{w['id']}")

    print()
    if failures:
        if skipped:
            print(
                f"CLI CONTRACT: {len(failures) - skipped} of {ran} check(s) failed, "
                f"and {skipped} never ran -- the suite hit its own budget\n"
            )
        else:
            print(f"CLI CONTRACT: {len(failures)} of {ran} check(s) failed\n")
        for section, name, detail in failures:
            print(f"  [{section}] {name}")
            for line in detail.splitlines()[:12]:
                print(f"      {line}")
            print()
        print("  These are the endpoints docs/tfe-cli-surface.md calls a stable")
        print("  contract for terraform/tofu. A break here is a break for a CLI")
        print("  already installed on somebody's machine, which no later release")
        print("  can fix for them.")
        return 1

    print(f"CLI CONTRACT: OK -- {ran} check(s) against the real binary")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
