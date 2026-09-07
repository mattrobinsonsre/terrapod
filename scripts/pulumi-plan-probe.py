#!/usr/bin/env python3
"""Probe what `pulumi up --plan` actually enforces (#1501, part of #1407).

Fifth in the series after the four protocol captures, and for the same reason
those exist: #1407 §3 rests an entire approval model on the claim that
`pulumi up --plan` refuses operations outside the saved plan, and that claim was
carried for a long time as "verified from docs, **not run**".

Running it changed the design. The refusal is real, but it binds *less* than
`tfplan` does in one specific way, and no amount of reading turns that up:

    the plan constrains WHICH RESOURCES are operated on, with which step kinds,
    and the DESIRED INPUTS — but NOT the state the update starts from.

A plan saved for `12 -> 20` still applies cleanly after state has moved out of
band to 31, and produces 20. Terraform refuses a stale plan on a state-serial
mismatch; Pulumi has no equivalent check, so Terrapod has to supply one.

Everything else held: no `PULUMI_EXPERIMENTAL`, refusal on both create and
update plans, refusal costs nothing (it is caught in `up`'s preview phase, so
state is untouched), it fails on a *missing* planned operation as well as an
extra one, the plan is portable across a pod boundary, and it carries secrets
encrypted under the stack's secrets provider rather than in the clear.

    python3 scripts/pulumi-plan-probe.py

Requires Docker (for the `pulumi/pulumi-python` image); stdlib only otherwise.
No cloud credentials: the program uses `pulumi-random` and a local file backend.

Each refusal below is paired with re-applying the SAME plan file against the
restored program. Without that control a failure only proves plans are broken,
not that the refusal is doing anything.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile

IMAGE = "pulumi/pulumi-python:latest"

PROGRAM = '''\
"""Two resources, no cloud credentials.

`RandomPassword.result` is a *secret* output, which is what lets this answer
whether a saved plan carries secrets in the clear.
"""
import pulumi
import pulumi_random as random

length = pulumi.Config().get_int("length") or 8

s = random.RandomString("s", length=length, special=False)
pw = random.RandomPassword("pw", length=20)

pulumi.export("s", s.result)
pulumi.export("pw", pw.result)
'''

PULUMI_YAML = """\
name: planprobe
runtime: python
description: Probe for terrapod #1501
"""

REQUIREMENTS = "pulumi>=3.0.0\npulumi-random>=4.0.0\n"

#: Driven entirely inside one container so the file-backend state survives
#: between steps. `pulumi login --local` would put state in the container's
#: ephemeral HOME rather than the mount, which silently loses the stack.
SCRIPT = r"""
set -u
cd /work/proj
export PULUMI_HOME=/work/home PULUMI_CONFIG_PASSPHRASE=probe PULUMI_SKIP_UPDATE_CHECK=true
python -m venv /work/venv >/dev/null 2>&1
export PATH=/work/venv/bin:$PATH
/work/venv/bin/pip install -q -r requirements.txt >/dev/null 2>&1
pulumi login file:///work/state >/dev/null 2>&1
pulumi stack init dev --non-interactive >/dev/null 2>&1

echo "PULUMI_EXPERIMENTAL=[${PULUMI_EXPERIMENTAL:-<unset>}]  version=$(pulumi version)"

echo "--- 1. preview --save-plan (length=8)"
pulumi preview --save-plan=/work/plan.json --non-interactive >/dev/null 2>&1
echo "    exit=$?  plan written=$(test -f /work/plan.json && echo yes || echo no)"

echo "--- 2. mutate to 99, apply the STALE plan  (expect refusal)"
sed -i 's/or 8$/or 99/' __main__.py
pulumi up --plan=/work/plan.json --yes --non-interactive >/work/o.txt 2>&1
echo "    exit=$?"
grep -o 'violates plan:.*' /work/o.txt | head -1 | sed 's/^/    /'
grep -qi 'Previewing update' /work/o.txt && echo "    caught in the PREVIEW phase -> nothing was mutated"

echo "--- 3. CONTROL: restore to 8, apply the SAME plan  (expect success)"
sed -i 's/or 99$/or 8/' __main__.py
pulumi up --plan=/work/plan.json --yes --non-interactive >/dev/null 2>&1
echo "    exit=$?"

echo "--- 4. secrets: what does the plan carry?"
python - <<'PY'
import json
d = json.load(open("/work/plan.json"))
raw = json.dumps(d)
print("    secret sentinel present:", "4dabf18193072939515e22adb298388d" in raw)
print("    ciphertext present:     ", "ciphertext" in raw)
print("    plaintext present:      ", "plaintext" in raw)
PY

echo "--- 5. state drift: save a plan, change state OUT OF BAND, then apply the plan"
sed -i 's/or 8$/or 20/' __main__.py
pulumi preview --save-plan=/work/plan2.json --non-interactive >/dev/null 2>&1
sed -i 's/or 20$/or 31/' __main__.py
pulumi up --yes --non-interactive >/dev/null 2>&1          # out of band, no --plan
sed -i 's/or 31$/or 20/' __main__.py
pulumi up --plan=/work/plan2.json --yes --non-interactive >/dev/null 2>&1
echo "    exit=$?   <- 0 means drift does NOT invalidate a saved plan"
echo "    s is now $(pulumi stack output s | tr -d '\n' | wc -c | tr -d ' ') chars"

echo "--- 6. a planned operation that is no longer needed (expect refusal)"
sed -i 's/or 20$/or 25/' __main__.py
pulumi preview --save-plan=/work/plan3.json --non-interactive >/dev/null 2>&1
pulumi up --yes --non-interactive >/dev/null 2>&1          # out of band, same change
pulumi up --plan=/work/plan3.json --yes --non-interactive >/work/o3.txt 2>&1
echo "    exit=$?"
grep -o 'expected resource operations.*' /work/o3.txt | head -1 | sed 's/^/    /'
"""


def main() -> int:
    if shutil.which("docker") is None:
        print("docker is required (for the pulumi image)", file=sys.stderr)
        return 2

    work = pathlib.Path(tempfile.mkdtemp(prefix="pulumi-plan-probe-"))
    proj = work / "proj"
    proj.mkdir()
    (proj / "Pulumi.yaml").write_text(PULUMI_YAML)
    (proj / "requirements.txt").write_text(REQUIREMENTS)
    (proj / "__main__.py").write_text(PROGRAM)
    (work / "run.sh").write_text(SCRIPT)
    (work / "state").mkdir()

    # Fixed argv and no inherited environment: nothing here is caller-controlled.
    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{work}:/work",
            "-w",
            "/work",
            IMAGE,
            "bash",
            "/work/run.sh",
        ],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    print(proc.stdout)
    if proc.returncode != 0:
        for line in proc.stderr.strip().splitlines()[-5:]:
            print(f"  ! {line}", file=sys.stderr)
    shutil.rmtree(work, ignore_errors=True)

    print("Compare against the findings in #1501; update #1407 §3 if pulumi has moved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
