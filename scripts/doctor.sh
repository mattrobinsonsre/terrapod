#!/usr/bin/env bash
# Report the health of the local container store.
#
# Exists because of a failure mode that is invisible until it is expensive.
# `podman build` writes a layer's content to disk and then commits its metadata.
# Interrupt it between the two — a killed build, a timed-out CI step, a full
# disk — and the layer stays on disk with nothing referencing it.
#
# Nothing reclaims those. `podman system prune` only removes objects podman has
# metadata for, and `podman system check --repair` likewise cannot see a layer
# it has no record of; it reports zero even with `--max 1m`. The space is gone
# short of a storage reset.
#
# It also accelerates: as the disk fills, metadata writes start failing, which
# orphans layers that would otherwise have committed cleanly.
#
# One workstation reached 9,328 orphaned layers holding ~85 GB before anyone
# noticed, because every individual symptom looked like an ordinary build
# failure. This makes the drift visible while it is still small.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

info "Container store health"

if ! command -v podman >/dev/null 2>&1; then
  warn "podman not found — nothing to check (this diagnostic is podman-specific)."
  exit 0
fi

if ! podman machine ssh true >/dev/null 2>&1; then
  warn "No running podman machine — skipping."
  exit 0
fi

# ── Space ─────────────────────────────────────────────────
echo
podman machine ssh 'df -h /var/lib/containers 2>/dev/null | tail -1' 2>/dev/null \
  | awk '{ printf "  disk:        %s used of %s (%s), %s free\n", $3, $2, $5, $4 }'

# ── Tracked vs on disk ────────────────────────────────────
# The number that matters. `podman system df` reports only what podman knows
# about, so it looks healthy while the store is full of layers it has forgotten.
orphans="$(podman machine ssh 'sudo python3 -c "
import json, os
root = \"/var/lib/containers/storage/overlay\"
try:
    tracked = {l[\"id\"] for l in json.load(open(\"/var/lib/containers/storage/overlay-layers/layers.json\"))}
except Exception:
    tracked = set()
dirs = [d for d in os.listdir(root) if len(d) == 64]
print(len(dirs), len(tracked), len([d for d in dirs if d not in tracked]))
"' 2>/dev/null || true)"

set -- $orphans
on_disk="${1:-}"; tracked="${2:-}"; orphaned="${3:-}"

if [ -z "$orphaned" ]; then
  warn "Could not read the layer store (needs a running podman machine with sudo)."
else
  printf '  layers:      %s on disk, %s tracked, %s ORPHANED\n' "$on_disk" "$tracked" "$orphaned"
  echo
  if [ "$orphaned" -gt 500 ]; then
    error "$orphaned orphaned layers. These are unreachable by 'podman system prune'"
    error "and by 'podman system check --repair' — neither can see a layer with no"
    error "metadata entry. Reclaiming them means 'podman system reset', which also"
    error "destroys any local k3d cluster; see docs/local-development.md to rebuild."
    exit 1
  elif [ "$orphaned" -gt 50 ]; then
    warn "$orphaned orphaned layers and growing. Worth resetting the store at a"
    warn "convenient moment, before it becomes expensive."
  else
    success "Container store is healthy."
  fi
fi

# ── Damaged tracked layers ────────────────────────────────
# Separate problem, separate fix: these HAVE metadata, so --repair can act.
echo
info "Checking tracked layers for damage (this is slow; ^C to skip)"
if podman system check --quick >/dev/null 2>&1; then
  success "No damage reported in tracked layers."
else
  warn "Damaged tracked layers found. Unlike orphans these are repairable:"
  warn "  podman system check --repair"
fi
