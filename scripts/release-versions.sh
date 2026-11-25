#!/usr/bin/env bash
#
# The Terrapod releases among a list of version strings, in semver order.
#
# Reads candidate version strings on stdin -- git tags, GitHub release tag
# names, anything -- and writes the ones that are Terrapod releases to stdout,
# OLDEST FIRST, one per line. With `--before VERSION` it writes only those
# strictly older than VERSION. Either way the caller takes `| tail -1` to get
# the newest of whatever it asked for.
#
# This is the ONE producer of "which published versions are ours, and which of
# two is newer". Two steps of `release-finalize` in `.github/workflows/ci.yml`
# read it: the "Latest" badge election, and the release notes' compare base.
#
# The notes base is the reason it exists. It used to ask `git describe --tags
# --abbrev=0 "${version}^"`, which answers "the nearest tag of ANY kind" -- and
# this repository also publishes Checkov builds, tagged `3.4.4` and the like
# (38 of its 311 tags when this was written). So that command for
# `v2.0.0-beta.6` returned `3.4.4`, and the generated notes linked
# `compare/3.4.4...v2.0.0-beta.6`: a range spanning a Checkov build's history,
# with the commit list drawn from that same wrong range (#2702). It is
# INTERMITTENT, which is worse than consistent -- the same command for
# `v2.0.0-beta.7` returned `v2.0.0-beta.6`, correctly, because no Checkov tag
# happened to sit in between. And the notes are hand-replaced on every release,
# so a practice rather than a gate was the only thing between that and a
# published "Full Changelog" link spanning the wrong range.
#
# Two subtleties live here so that neither caller has to restate them, and so
# that they cannot drift apart:
#
#   * The pattern is anchored at BOTH ends, which is what keeps the Checkov
#     tags out of the comparison. Its second alternative admits a pre-release
#     on a MAJOR only (`v2.0.0-alpha.1`), because a pre-release is for a major
#     release alone (#2277): the tag trigger and the signing identity are held
#     to that same rule, and `scripts/docs-audit/check_release_tag_triggers.py`
#     checks that they agree.
#
#   * `sed 's/-/~/'` on the FIRST dash is what makes `sort -V` agree with
#     semver, and it is not cosmetic. `sort -V` is Debian version ordering,
#     where a pre-release suffix sorts AFTER the release it precedes --
#     measured identically under GNU coreutils 9.12 and BSD sort, so it is not
#     a platform quirk to be waited out. Untransformed, `v2.0.0-alpha.1` beats
#     `v2.0.0`, and the alpha would keep the badge for ever once 2.0.0 shipped.
#     `~` is the one character that sorts before nothing, so the substitution
#     restores exactly semver's rule; it exists only inside this script and
#     never reaches a tag, which matters because `~` is neither valid semver
#     nor a valid Docker tag.
#
# An empty result is a legitimate answer and exits 0: the first release of all
# has no predecessor, and each caller's own emptiness check is what handles
# that. Exiting non-zero would abort a release step instead.
#
# `scripts/docs-audit/check_release_notes_base.py` drives this script over
# literal fixtures, and checks that both call sites route through it.
set -eu

# Anchored at both ends; see above. Written once, here.
PATTERN='^v[0-9]+\.[0-9]+\.[0-9]+$|^v[0-9]+\.0\.0-[0-9A-Za-z.-]+$'

before=""
case "${1-}" in
  "")
    ;;
  --before)
    before="${2-}"
    if [ -z "$before" ]; then
      echo "release-versions.sh: --before needs a version" >&2
      exit 2
    fi
    ;;
  *)
    echo "release-versions.sh: unknown argument '$1'" >&2
    exit 2
    ;;
esac

candidates=$(cat)

if [ -n "$before" ]; then
  # Injected rather than required of the caller, so the truncation below always
  # has something to stop at however the caller enumerated its candidates.
  candidates="${candidates}
${before}"
fi

# `|| true` because grep exits 1 on no matches, and no matches is an answer
# rather than an error. pipefail is deliberately NOT set: the `awk ... { exit }`
# below closes its pipe early by design, and under pipefail the SIGPIPE on the
# writing end would surface as a failure.
ordered=$(
  printf '%s\n' "$candidates" \
    | { grep -E "$PATTERN" || true; } \
    | sed 's/-/~/' \
    | sort -V \
    | sed 's/~/-/'
)

if [ -z "$ordered" ]; then
  exit 0
fi

if [ -z "$before" ]; then
  printf '%s\n' "$ordered"
  exit 0
fi

# Refuse rather than quietly emit the whole list. Were VERSION not itself a
# Terrapod release it would survive no filter, the truncation would stop at
# nothing, and the caller's `tail -1` would hand back the newest release of all
# -- possibly NEWER than the version being released, which is wrong in the one
# direction nothing downstream could detect.
if ! printf '%s\n' "$ordered" | grep -qxF "$before"; then
  echo "release-versions.sh: --before '$before' is not a Terrapod release version" >&2
  exit 2
fi

# Everything up to the first occurrence of VERSION is strictly older, because
# the list is sorted. `NF` drops the blank line that a `printf` of an empty
# string would otherwise produce.
printf '%s\n' "$ordered" | awk -v v="$before" '$0 == v { exit } NF { print }'
