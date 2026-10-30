#!/usr/bin/env bash
# Does a configure's SSH transport actually work, and does it fail closed?
#
# WHY THIS EXISTS. Every playbook exercise in this repository was
# `connection: local`, and the only `connection: ssh` host in the tree is
# loopback on port 1 -- engineered to be UNREACHABLE so ansible's unreachable
# callback fires. So the SSH transport, the key file the machine credential
# writes, and host-key verification were implemented, unit-tested, and had never
# once been executed in the success direction (#2318).
#
# WHY IT NEEDS NO KUBERNETES. The property under test is the EE image's SSH
# behaviour, which is a property of the image and ansible's defaults -- not of
# the Job. Two containers on a docker network settle it in seconds, which is
# what makes this a gate rather than a manual smoke. The Job wiring (sidecar,
# volumes, credential plumbing, fsGroup) is a separate proof on the Tilt stack,
# because that part genuinely cannot be shown here.
#
# THE ROW THAT MATTERS IS THE FAILING ONE. `unknown host key -> refused` is what
# stops a future change from silently disabling verification; the three
# succeeding rows only prove the probe can succeed at all, which is what makes
# the refusal meaningful rather than a fixture that was broken all along.
set -euo pipefail

EE_IMAGE="${EE_IMAGE:-ghcr.io/mattrobinsonsre/terrapod-ee:2.21}"
NET="tp-sshgate-$$"
TARGET="tp-sshgate-target-$$"
WORK="$(mktemp -d)"
FAILURES=0

cleanup() {
  docker stop "$TARGET" >/dev/null 2>&1 || true
  docker container rm "$TARGET" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  [ -n "${WORK:-}" ] && [ -d "$WORK" ] && find "$WORK" -mindepth 1 -delete 2>/dev/null || true
  [ -n "${WORK:-}" ] && [ -d "$WORK" ] && rmdir "$WORK" 2>/dev/null || true
}
trap cleanup EXIT

#: Derived from the row banners rather than written. This summary said "four"
#: after two rows were added -- a count in prose with nothing gating it, which
#: is exactly what it is now derived to avoid.
ROWS="$(grep -c '^note "row [0-9]*/' "$0")"

note() { printf '  %s\n' "$*"; }
fail() { printf '  FAIL: %s\n' "$*"; FAILURES=$((FAILURES + 1)); }

# ── fixture ──────────────────────────────────────────────────────────────────
# A keypair per run, never committed: a private key in a public repository is a
# secret-scanner finding whatever its intended lifetime.
ssh-keygen -q -t ed25519 -N '' -f "$WORK/id" -C "terrapod-ssh-gate"
mkdir -p "$WORK/akeys"
cp "$WORK/id.pub" "$WORK/akeys/target"

# The EE runs as uid 1000 and `mktemp -d` is 0700 owned by whoever runs this, so
# without this the container cannot even TRAVERSE the mount and every row fails
# with `cannot open /work/probe.sh: Permission denied` -- which reads as
# host-key verification having regressed rather than as a uid boundary.
#
# It passed locally and failed in CI for exactly this reason: a macOS container
# mount does not enforce the uid boundary a Linux bind mount does, so the
# difference is the runner, not the product. The key is generated per run and
# the runner is ephemeral, and the probe copies it to 0600 inside the container
# before ssh will look at it.
chmod 0755 "$WORK" "$WORK/akeys"
chmod 0644 "$WORK/id" "$WORK/id.pub" "$WORK/akeys/target"

docker build -q -t terrapod-ssh-target:gate -f docker/Dockerfile.ssh-target docker/ >/dev/null
docker network create "$NET" >/dev/null
docker run -d --name "$TARGET" --network "$NET" \
  -v "$WORK/akeys:/etc/ssh/authorized_keys.d:ro" \
  terrapod-ssh-target:gate >/dev/null
note "target up: $TARGET"

# The host key, captured the way an operator captures a real host's.
PUB="$(docker exec "$TARGET" cat /etc/ssh/ssh_host_ed25519_key.pub)"
printf '%s %s %s\n' "$TARGET" "$(echo "$PUB" | awk '{print $1}')" "$(echo "$PUB" | awk '{print $2}')" \
  > "$WORK/known_hosts"

cat > "$WORK/inv.yml" <<INV
all:
  hosts:
    $TARGET:
      ansible_user: target
      ansible_ssh_private_key_file: /tmp/k
INV

cat > "$WORK/play.yml" <<'PLAY'
- hosts: all
  gather_facts: false
  tasks:
    - name: a task that cannot succeed without a real connection
      ansible.builtin.raw: id -un
      register: who
    - name: and one that changes something on the target
      ansible.builtin.raw: touch /home/target/terrapod-was-here
    - ansible.builtin.debug:
        msg: "TERRAPOD_SSH_OK as {{ who.stdout | trim }}"
PLAY

# A second playbook that stages a real MODULE, for rows 5 and 6. Separate from
# `play.yml` so rows 1 to 4 keep asserting exactly what they asserted before: a
# module task in the shared playbook would make every row depend on module
# staging as well as on the transport, and a failure could then mean either.
cat > "$WORK/play-module.yml" <<'PLAY'
- hosts: all
  gather_facts: false
  tasks:
    - name: a real MODULE, which cannot run without a staged interpreter
      ansible.builtin.file:
        path: /home/target/terrapod-module-was-here
        state: touch
    - name: and one that reads a fact back through the module path
      ansible.builtin.command: id -un
      register: who
      changed_when: false
    - ansible.builtin.debug:
        msg: "TERRAPOD_MODULE_OK as {{ who.stdout | trim }}"
PLAY

cat > "$WORK/probe.sh" <<'PROBE'
#!/bin/sh
cp /work/id /tmp/k
chmod 600 /tmp/k
exec ansible-playbook -i /work/inv.yml "${PLAY:-/work/play.yml}"
PROBE
chmod 0755 "$WORK/probe.sh"
chmod 0644 "$WORK/inv.yml" "$WORK/play.yml" "$WORK/play-module.yml" "$WORK/known_hosts"

# ── the four rows ────────────────────────────────────────────────────────────
probe() {
  docker run --rm --network "$NET" --entrypoint sh \
    -v "$WORK:/work:ro" -e HOME=/tmp \
    -e ANSIBLE_REMOTE_TEMP="${REMOTE_TMP:-~/.ansible/tmp}" \
    -e PLAY="${PLAY_FILE:-/work/play.yml}" \
    ${EXTRA:+-e "$EXTRA"} "$EE_IMAGE" /work/probe.sh 2>&1 || true
}

note "row 1/6: an unknown host key must be REFUSED"
EXTRA="" OUT="$(probe)"
if echo "$OUT" | grep -q 'Host key verification failed'; then
  note "        refused, as it must be"
else
  fail "an unknown host key was NOT refused -- host-key verification has regressed"
  echo "$OUT" | tail -5 | sed 's/^/        /'
fi
if echo "$OUT" | grep -q 'TERRAPOD_SSH_OK'; then
  fail "the play CONNECTED to a host whose key was never trusted"
fi

note "row 2/6: the operator supplies the host key -> connects"
EXTRA="ANSIBLE_SSH_ARGS=-o UserKnownHostsFile=/work/known_hosts" OUT="$(probe)"
if echo "$OUT" | grep -q 'TERRAPOD_SSH_OK as target'; then
  note "        connected, with verification left ON"
else
  fail "a supplied host key did not produce a connection"
  echo "$OUT" | tail -8 | sed 's/^/        /'
fi

note "row 3/6: ANSIBLE_HOST_KEY_CHECKING=False -> connects"
EXTRA="ANSIBLE_HOST_KEY_CHECKING=False" OUT="$(probe)"
echo "$OUT" | grep -q 'TERRAPOD_SSH_OK as target' \
  && note "        connected" \
  || fail "the documented disable route did not connect"

note "row 4/6: -o StrictHostKeyChecking=no -> connects"
EXTRA="ANSIBLE_SSH_ARGS=-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null" OUT="$(probe)"
echo "$OUT" | grep -q 'TERRAPOD_SSH_OK as target' \
  && note "        connected" \
  || fail "the per-host disable route did not connect"

# ── module staging, which `raw` cannot reach ─────────────────────────────────
# Rows 1 to 4 prove the TRANSPORT. Every one of them uses `ansible.builtin.raw`,
# which needs no interpreter and no temp directory on the target -- so until now
# no gate in this repository had ever staged a module over SSH, and the whole
# of `ANSIBLE_REMOTE_TEMP` above was inert. These two rows are that gap, in both
# directions, because the success row alone cannot show the setting is read.
note "row 5/6: a real MODULE stages and runs -> succeeds"
PLAY_FILE=/work/play-module.yml \
  EXTRA="ANSIBLE_SSH_ARGS=-o UserKnownHostsFile=/work/known_hosts" OUT="$(probe)"
if echo "$OUT" | grep -q 'TERRAPOD_MODULE_OK as target'; then
  note "        staged and ran, with verification left ON"
else
  fail "a real module did not run -- the target cannot stage one, so every other row here proves the transport alone"
  echo "$OUT" | tail -8 | sed 's/^/        /'
fi

note "row 6/6: an UNWRITABLE remote_tmp makes it UNREACHABLE, not failed"
# The recorded trap, asserted rather than remembered: ansible resolves
# `remote_tmp`'s leading `~` against the TARGET user's passwd entry, so a target
# whose home is unwritable fails every task as UNREACHABLE with a message that
# points away from the cause. `/proc` is unwritable on any Linux target, which
# makes this structural rather than a permission the fixture could change.
PLAY_FILE=/work/play-module.yml REMOTE_TMP=/proc/terrapod-cannot-write \
  EXTRA="ANSIBLE_SSH_ARGS=-o UserKnownHostsFile=/work/known_hosts" OUT="$(probe)"
if echo "$OUT" | grep -q 'TERRAPOD_MODULE_OK'; then
  fail "a module ran with an unwritable remote_tmp -- ANSIBLE_REMOTE_TEMP is not being read, so row 5 proves less than it appears to"
elif echo "$OUT" | grep -qE 'UNREACHABLE|Failed to create temporary directory|not writable'; then
  note "        refused, and the setting is therefore live"
else
  fail "an unwritable remote_tmp failed for some OTHER reason -- read the output rather than trusting this row"
  echo "$OUT" | tail -8 | sed 's/^/        /'
fi

# A control on the fixture itself: the change must have REACHED the target,
# so a play that reports ok= on a host it never touched cannot pass this gate.
if docker exec "$TARGET" test -f /home/target/terrapod-was-here; then
  note "control: the task's effect is present ON THE TARGET"
else
  fail "no connection ever changed anything on the target -- every 'ok' above is hollow"
fi

if [ "$FAILURES" -ne 0 ]; then
  printf '\n  %s row(s) failed\n' "$FAILURES"
  exit 1
fi
printf '\n  all %s rows hold, and the effect reached the target\n' "$ROWS"
