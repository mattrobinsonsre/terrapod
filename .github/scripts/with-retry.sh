#!/usr/bin/env bash
# Run a command with exponential-backoff retry for transient infrastructure flakes.
#
# Usage:   .github/scripts/with-retry.sh "<label>" -- cmd args...
#
# Five attempts at 5s, 10s, 20s, 40s intervals (total cap ~75s). Emits
# GitHub Actions `::warning::` lines between attempts and `::error::`
# on terminal failure so flakes show up annotated in the run summary.
#
# Used from .github/workflows/ci.yml for GHCR-touching ops that the
# upstream action / CLI doesn't retry itself (docker login is wrapped
# by .github/actions/ghcr-login; this script covers `docker buildx
# imagetools create`, `helm push`, etc.).
set -u

if [ "$#" -lt 3 ] || [ "$2" != "--" ]; then
  echo "usage: with-retry.sh '<label>' -- <command> [args...]" >&2
  exit 2
fi

label="$1"
shift 2  # drop label + '--'

for i in 1 2 3 4 5; do
  if "$@"; then
    exit 0
  fi
  if [ "$i" -eq 5 ]; then
    echo "::error::$label failed after 5 attempts"
    exit 1
  fi
  wait=$(( 5 * (2 ** (i - 1)) ))
  echo "::warning::$label attempt $i failed; retrying in ${wait}s"
  sleep "$wait"
done
