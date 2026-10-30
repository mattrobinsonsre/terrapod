#!/usr/bin/env bash
# Can the execution environment clone a git-sourced Ansible role with the
# workspace's own credential -- and does it still verify the host key?
#
# WHY THIS EXISTS. `git_auth` wrote a per-host key, a `known_hosts`, an ssh
# config with `StrictHostKeyChecking yes` and a gitconfig wiring them in through
# `core.sshCommand` -- all under the ORCHESTRATOR's `$HOME`, which the container
# that actually runs `ansible-galaxy` cannot read. So an in-house role, which is
# the common case rather than a corner, could not be installed from anywhere
# that needs a credential, and the code's own comments claimed the opposite
# (#2320). The fix puts the material on the shared volume; this proves it works.
#
# WHY IT DRIVES THE REAL `git_auth` RATHER THAN A HAND-WRITTEN GITCONFIG. A
# fixture config would test the fixture. The property under test is that the
# material `git_auth` ACTUALLY writes, placed where the fix places it, is enough
# for the EE's git -- so the gate imports the phase and calls it, and a change to
# what it emits is a change this gate sees.
#
# WHY IT NEEDS NO KUBERNETES. The shared volume's defining property is that it is
# mounted at the SAME ABSOLUTE PATH in both containers, which is what lets one
# copy of a config full of absolute paths serve both. A bind mount at an
# identical path reproduces exactly that, which is why this is a gate rather than
# a manual smoke. The Job wiring -- the sidecar, `TP_EE_GIT_DIR`,
# `GIT_CONFIG_GLOBAL` on the EE -- is asserted in the runner tier and the Job
# spec golden; what cannot be shown there is whether git and ansible-galaxy
# actually accept the result.
#
# THE ROW THAT MATTERS IS THE FAILING ONE. `no credential -> refused` is what
# stops a future change from accidentally making the fixture world-readable and
# turning every other row into a tautology. The succeeding rows only prove the
# probe CAN succeed, which is what makes the refusal meaningful.
set -euo pipefail

EE_IMAGE="${EE_IMAGE:-ghcr.io/mattrobinsonsre/terrapod-ee:2.21}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NET="tp-gitgate-$$"
TARGET="tp-gitgate-target-$$"
# Deliberately NOT `mktemp -d`: the generated gitconfig carries ABSOLUTE paths,
# so the directory has to be mountable at its own path inside the container --
# which is the shared volume's defining property, reproduced rather than
# simulated. A path under the repo root is one both sides can name.
WORK="$REPO_ROOT/.gitgate-$$"
FAILURES=0

# `chown -R 1000:1000` below makes the materialised tree unremovable by the
# caller: a GitHub runner is uid 1001, so `rm -rf` on it dies `Permission
# denied`. On macOS the container mount does not enforce that boundary, so a
# plain `rm` passes locally and fails in CI -- which is exactly how this gate
# first went red. One producer, used by the trap and by row 5.
rm_as_root() {
  docker run --rm -v "$1:/t" --entrypoint sh alpine:3.21 \
    -c 'rm -rf /t/..?* /t/.[!.]* /t/*' >/dev/null 2>&1 || true
}

cleanup() {
  docker stop "$TARGET" >/dev/null 2>&1 || true
  docker container rm "$TARGET" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  # Owned by uid 1000 after the chown below, so remove it as that uid rather
  # than failing half-way and leaving material behind.
  if [ -d "$WORK" ]; then
    rm_as_root "$WORK"
    rmdir "$WORK" 2>/dev/null || true
  fi
}
trap cleanup EXIT

note() { printf '  %s\n' "$*"; }
fail() { printf '  FAIL: %s\n' "$*"; FAILURES=$((FAILURES + 1)); }

mkdir -p "$WORK"

# ── the git server ───────────────────────────────────────────────────────────
# A keypair per run, never committed: a private key in a public repository is a
# secret-scanner finding whatever its intended lifetime.
ssh-keygen -q -t ed25519 -N '' -f "$WORK/id" -C "terrapod-git-gate"
mkdir -p "$WORK/akeys"
cp "$WORK/id.pub" "$WORK/akeys/git"
chmod 0755 "$WORK" "$WORK/akeys"
chmod 0644 "$WORK/id" "$WORK/id.pub" "$WORK/akeys/git"

docker build -q -t terrapod-git-target:gate -f docker/Dockerfile.git-target docker/ >/dev/null
docker network create "$NET" >/dev/null
docker run -d --name "$TARGET" --network "$NET" \
  -v "$WORK/akeys:/etc/ssh/authorized_keys.d:ro" \
  terrapod-git-target:gate >/dev/null
note "git server up: $TARGET"

# The host key, captured the way an operator captures a real host's for the
# `known_hosts` field of a `git_ssh_auth` credential.
PUB="$(docker exec "$TARGET" cat /etc/ssh/ssh_host_ed25519_key.pub)"
HOSTKEY="$TARGET $(echo "$PUB" | awk '{print $1}') $(echo "$PUB" | awk '{print $2}')"

# ── the credential, materialised by the REAL phase ───────────────────────────
# `base_dir` is `$WORK/tp/git` on the host and is bind-mounted at that same
# absolute path in the container, so every absolute path the phase writes into
# the gitconfig and the ssh config resolves identically on both sides. That is
# the production arrangement, not an approximation of it.
GIT_DIR="$WORK/tp/git"
PY="${PYTHON:-python3}"
REPO_ROOT="$REPO_ROOT" "$PY" - "$GIT_DIR" "$TARGET" "$HOSTKEY" <<'PYEOF'
import json, os, sys
from pathlib import Path

# The repo's own module, imported from `services/`, so this gate exercises what
# the runner runs rather than a transcription of it.
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "services"))
from terrapod.runner.phases.git_auth import configure

git_dir, host, hostkey = sys.argv[1], sys.argv[2], sys.argv[3]
key = Path(git_dir).parent.parent / "id"
entries = [
    {
        "category": "git_ssh_auth",
        # The scope pattern an operator writes for this host. `_host_of` reads
        # the host out of it, and every generated filename is derived from that.
        "key": f"ssh://git@{host}/",
        "value": json.dumps(
            {"private_key": key.read_text(), "known_hosts": hostkey, "rewrite": "none"}
        ),
    }
]
env = configure(entries, base_dir=Path(git_dir))
if not env:
    raise SystemExit("git_auth.configure returned {} -- it refused the entry")
# Hand the caller exactly what the orchestrator merges into its environment, so
# the probe sets the same variables the real run does rather than a chosen
# subset.
Path(git_dir, "gate-env").write_text(
    "".join(f"{k}={v}\n" for k, v in sorted(env.items())), encoding="utf-8"
)
print(f"  git_auth wrote {len(list(Path(git_dir).rglob('*')))} files under {git_dir}")
PYEOF

# The EE runs as uid 1000 and `git_auth` writes its keys 0600, so the material
# has to BELONG to 1000 -- which is exactly how it looks in production, where
# both containers are that uid (#1928 pinned the EE's). Widening the modes
# instead would both diverge from production and make ssh refuse the key as
# "UNPROTECTED PRIVATE KEY FILE".
docker run --rm -v "$WORK:/w" --entrypoint sh alpine:3.21 \
  -c 'chown -R 1000:1000 /w/tp && chmod 0755 /w /w/tp' >/dev/null
note "credential materialised and owned by uid 1000, modes left as the phase wrote them"

# ── the probe ────────────────────────────────────────────────────────────────
# `requirements.yml` is written per row, because the `src:`/`scm:` pair IS what
# each row varies.
probe() {
  cat > "$WORK/requirements.yml" <<REQ
roles:
$ROLE_ENTRY
REQ
  chmod 0644 "$WORK/requirements.yml"
  docker run --rm --network "$NET" --entrypoint sh \
    -v "$WORK/tp:$WORK/tp:ro" -v "$WORK/requirements.yml:/req.yml:ro" \
    -e HOME=/tmp -e TMPDIR=/tmp \
    ${WITH_CREDENTIAL:+--env-file "$GIT_DIR/gate-env"} \
    "$EE_IMAGE" -c 'ansible-galaxy role install -r /req.yml -p /tmp/roles 2>&1; echo "RC=$?"' 2>&1 || true
}

installed() { echo "$1" | grep -q 'was installed successfully'; }

note "row 1/5: NO credential -> the clone must be REFUSED"
ROLE_ENTRY="  - src: ssh://git@$TARGET/srv/role.git
    name: gaterole
    scm: git" WITH_CREDENTIAL="" OUT="$(probe)"
if installed "$OUT"; then
  fail "a role installed from an authenticating server with NO credential -- the fixture is not actually protected, so every other row below is a tautology"
else
  note "        refused, as it must be"
fi

note "row 2/5: git+ssh:// WITH the credential -> installs"
ROLE_ENTRY="  - src: ssh://git@$TARGET/srv/role.git
    name: gaterole
    scm: git" WITH_CREDENTIAL=1 OUT="$(probe)"
if installed "$OUT"; then
  note "        installed, with the credential git_auth wrote"
else
  fail "the credential did not reach the EE -- this is the whole of #2320"
  echo "$OUT" | tail -8 | sed 's/^/        /'
fi

note "row 3/5: the SCP form with scm: git -> installs"
ROLE_ENTRY="  - src: git@$TARGET:/srv/role.git
    name: gaterole
    scm: git" WITH_CREDENTIAL=1 OUT="$(probe)"
if installed "$OUT"; then
  note "        installed -- it is a git reference and is supported"
else
  fail "the SCP form with scm: git did not install"
  echo "$OUT" | tail -8 | sed 's/^/        /'
fi

note "row 4/5: a scheme-less src with NO scm: resolves to Galaxy, and must not clone"
ROLE_ENTRY="  - src: git@$TARGET:/srv/role.git
    name: gaterole" WITH_CREDENTIAL=1 OUT="$(probe)"
if installed "$OUT"; then
  fail "a scheme-less src installed -- ansible's contract is that it names a Galaxy role, and the docs say so"
else
  note "        not cloned, which is ansible's own contract for a scheme-less src"
fi

note "row 5/5: host-key verification is still ON -- a WRONG known_hosts must refuse"
# Re-materialise with a host key that is syntactically valid and belongs to a
# different host, which is what a stale `known_hosts` field looks like. If the
# clone still succeeds, the credential was bought at the cost of verification --
# the one thing the acceptance forbids.
ssh-keygen -q -t ed25519 -N '' -f "$WORK/wrong" -C wrong
WRONGKEY="$TARGET $(awk '{print $1" "$2}' "$WORK/wrong.pub")"
rm_as_root "$WORK/tp"
rmdir "$WORK/tp" 2>/dev/null || true
REPO_ROOT="$REPO_ROOT" "$PY" - "$GIT_DIR" "$TARGET" "$WRONGKEY" <<'PYEOF' >/dev/null
import json, os, sys
from pathlib import Path
sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "services"))
from terrapod.runner.phases.git_auth import configure
git_dir, host, hostkey = sys.argv[1], sys.argv[2], sys.argv[3]
key = Path(git_dir).parent.parent / "id"
env = configure(
    [{"category": "git_ssh_auth", "key": f"ssh://git@{host}/",
      "value": json.dumps({"private_key": key.read_text(), "known_hosts": hostkey})}],
    base_dir=Path(git_dir),
)
Path(git_dir, "gate-env").write_text("".join(f"{k}={v}\n" for k, v in sorted(env.items())))
PYEOF
docker run --rm -v "$WORK:/w" --entrypoint sh alpine:3.21 \
  -c 'chown -R 1000:1000 /w/tp && chmod 0755 /w /w/tp' >/dev/null
ROLE_ENTRY="  - src: ssh://git@$TARGET/srv/role.git
    name: gaterole
    scm: git" WITH_CREDENTIAL=1 OUT="$(probe)"
if installed "$OUT"; then
  fail "a clone succeeded against a host whose key does NOT match the credential's known_hosts -- verification has been disabled to make the credential work"
else
  note "        refused -- StrictHostKeyChecking yes is intact"
fi

echo
if [ "$FAILURES" -eq 0 ]; then
  note "all 5 rows as expected"
else
  note "$FAILURES row(s) failed"
fi
exit "$FAILURES"
