#!/bin/sh
# The contract an Ansible execution environment must meet (#2154).
#
# ONE PRODUCER, two readers. `Dockerfile.ee` runs this at build time so a broken
# image cannot be published, and the published-image conformance job pipes the
# same bytes into `docker run` so the contract is re-checked against what is
# actually in the registry. Those are different questions: the moving `X.Y` tags
# are rebuilt, and an operator pulls what is published rather than what was once
# built.
#
# It is PIPED rather than COPYed in, for two reasons. The image deliberately
# ships no Terrapod artefacts of any kind -- that is what makes an EE
# insensitive to a Terrapod release -- and an operator-supplied environment will
# never carry this file, while being exactly the image most worth checking. So
# the Dockerfile bind-mounts it for one RUN and the job feeds it on stdin.
#
# Run it as the uid the image runs as. Asserting the contract as root proves
# nothing about the user that will actually execute a playbook: during #2154's
# first build `ansible --version` passed as root and failed for uid 1000, whose
# HOME was unwritable, and then failed again because that uid had no passwd
# entry at all. Both were invisible to a root check.
set -eu

fail() { echo "ee-contract: FAIL — $1" >&2; exit 1; }

# 1. The four binaries the agent drives, plus ansible itself. `command -v`
#    rather than `test -x`: PATH is what the agent resolves against.
for bin in ansible-playbook ansible-galaxy ansible ssh git; do
    command -v "$bin" >/dev/null 2>&1 || fail "$bin is not on PATH"
done

# 2. ansible must RUN, not merely exist. A console script whose shebang cannot
#    be resolved is present, executable, and fails with "not found" -- which is
#    how an sdist-only ansible-core installed without `wheel` presents.
ansible --version >/dev/null 2>&1 || fail "ansible is on PATH but will not run"

# 3. Exactly uid 1000, which the Job pins with `runAsUser` (#1928). Our own
#    images must satisfy whatever the agent REQUIRES, or one of them passes its
#    build and then fails its own startup probe -- and the agent refuses any
#    other uid, because the orchestrator and the EE exchange files on the shared
#    volume at 0600 and that only works while the two uids agree.
#
#    It was "non-root, and numeric" while the uid was unpinned and `fsGroup`
#    carried the access through the group. Narrowed with the contract.
uid="$(id -u)"
[ "$uid" != "0" ] || fail "the image runs as root; it must run as uid 1000"
[ "$uid" = "1000" ] || fail "the image runs as uid $uid; an EE must run as uid 1000"

# 4. A real playbook with a REAL module. `ansible.builtin.debug` is a
#    controller-side action plugin that opens no connection and would pass while
#    proving nothing, so this uses `command` and `stat` over a local connection.
work="${TMPDIR:-/tmp}/ee-contract.$$"
mkdir -p "$work/local"
trap 'rm -rf "$work"' EXIT
cat > "$work/play.yml" <<'PLAY'
- hosts: localhost
  gather_facts: false
  tasks:
    - command: /bin/true
    - stat: {path: /etc/hostname}
PLAY
# Temp paths are forced because a conforming image may have a read-only root
# filesystem: ansible exits rc 250 with "No usable temporary directory found"
# rather than naming the cause.
TMPDIR="$work" ANSIBLE_LOCAL_TEMP="$work/local" \
    ansible-playbook -i localhost, -c local "$work/play.yml" > "$work/out" 2>&1 \
    || { cat "$work/out" >&2; fail "the probe playbook did not run"; }
grep -q 'failed=0' "$work/out" || { cat "$work/out" >&2; fail "a task failed"; }
grep -q 'ok=2' "$work/out" || { cat "$work/out" >&2; fail "both tasks did not run"; }

echo "ee-contract: PASS — ansible $(ansible --version | head -1 | sed 's/^ansible \[\{0,1\}//;s/\]$//') as uid $uid"
