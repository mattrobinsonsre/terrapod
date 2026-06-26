#!/usr/bin/env bash
#
# Run the AI-analysis evaluation harness (#602) against a LIVE model.
#
# Uses the test image (which already has litellm + the app deps), overlays the
# live `services/terrapod` + `services/ai_eval` source so prompt edits take
# effect without an image rebuild, and forwards ambient model credentials from
# the host environment:
#   - bedrock/*   → AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN
#                   + a region (AI_EVAL_AWS_REGION | AWS_REGION | AWS_DEFAULT_REGION)
#   - anthropic/* → ANTHROPIC_API_KEY
#
# Reports land in ./reports/ai-eval (gitignored).
#
# Usage:
#   scripts/ai-eval.sh list
#   scripts/ai-eval.sh run --model bedrock/us.anthropic.claude-sonnet-4-6 -n 3
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${TEST_IMAGE:-terrapod-test:local}"
REGION="${AI_EVAL_AWS_REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}}"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  # The test image is Docker-GC'd between runs; rebuild it on demand so the
  # harness is self-sufficient (same image scripts/test.sh builds).
  echo "test image '$IMAGE' not found — building it..." >&2
  docker build -f "$REPO_ROOT/docker/Dockerfile.test" -t "$IMAGE" "$REPO_ROOT" >&2
fi

mkdir -p "$REPO_ROOT/reports/ai-eval"

exec docker run --rm \
  -v "$REPO_ROOT/services/terrapod:/app/terrapod" \
  -v "$REPO_ROOT/services/ai_eval:/app/ai_eval" \
  -v "$REPO_ROOT/reports/ai-eval:/app/reports/ai-eval" \
  -w /app \
  -e ANTHROPIC_API_KEY \
  -e AWS_ACCESS_KEY_ID \
  -e AWS_SECRET_ACCESS_KEY \
  -e AWS_SESSION_TOKEN \
  -e AWS_REGION="$REGION" \
  -e AWS_DEFAULT_REGION="$REGION" \
  "$IMAGE" python -m ai_eval "$@"
