#!/bin/sh
set -e

# Signal-forwarding entrypoint for Terrapod runner Jobs.
#
# Time-budgeted graceful shutdown for spot instance preemption:
#   T=0:       K8s sends SIGTERM → entrypoint forwards SIGINT to tofu/terraform
#   T=0→CHILD_GRACE: Graceful shutdown (writes state, releases lock)
#   T=CHILD_GRACE: Watchdog sends SIGKILL if tofu/terraform is still running
#   T=CHILD_GRACE→(GRACE-5): Artifact uploads (logs, state) with --max-time
#   T=GRACE:   K8s SIGKILL deadline
#
# SIGINT is used instead of SIGTERM because HashiCorp recommends it for
# container environments. A second signal triggers ungraceful abort which
# may skip state writing entirely — so we only send ONE signal, then
# escalate to SIGKILL via a watchdog.
#
# All API calls use TP_AUTH_TOKEN (short-lived runner token from K8s Secret).

CHILD_PID=""
WATCHDOG_PID=""

# Read the actual grace period from Helm config (passed via job_template.py)
TERMINATION_GRACE="${TP_TERMINATION_GRACE:-120}"
UPLOAD_BUDGET=25  # seconds reserved for artifact uploads
CHILD_GRACE=$((TERMINATION_GRACE - UPLOAD_BUDGET))
# Ensure CHILD_GRACE is at least 30s
if [ "$CHILD_GRACE" -lt 30 ]; then
    CHILD_GRACE=30
fi

forward_signal() {
    if [ -n "$CHILD_PID" ]; then
        echo "[entrypoint] Received signal, forwarding SIGINT to child PID $CHILD_PID (grace=${CHILD_GRACE}s)"
        # SIGINT triggers terraform's graceful shutdown: finish current API call,
        # write state, release lock, exit. Do NOT send SIGTERM — while terraform
        # handles it, SIGINT is HashiCorp's documented recommendation for containers.
        kill -INT "$CHILD_PID" 2>/dev/null || true

        # Watchdog: SIGKILL after CHILD_GRACE seconds if terraform hangs.
        # Do NOT send a second INT — terraform treats double-signal as ungraceful
        # abort which may skip state writing entirely.
        ( sleep "$CHILD_GRACE" && kill -KILL "$CHILD_PID" 2>/dev/null ) &
        WATCHDOG_PID=$!
    fi
}

trap forward_signal TERM QUIT

# Helper: wait for child process, handling signal interruption correctly.
# After a signal, `wait` returns immediately with 128+signum but the child
# is still running (it received SIGINT via the trap). We must wait again
# for the child to actually exit (or be killed by the watchdog).
wait_for_child() {
    wait "$CHILD_PID" || EXIT_CODE=$?

    # If wait was interrupted by signal (exit > 128), terraform is still running.
    # The trap handler already sent SIGINT + started a SIGKILL watchdog.
    if [ "$EXIT_CODE" -gt 128 ] 2>/dev/null; then
        echo "[entrypoint] Signal received (exit=$EXIT_CODE), waiting for $TP_BACKEND graceful shutdown..."
        wait "$CHILD_PID" 2>/dev/null
        ACTUAL_EXIT=$?
        # Clean up watchdog if terraform exited before timeout
        if [ -n "$WATCHDOG_PID" ]; then
            kill "$WATCHDOG_PID" 2>/dev/null || true
            wait "$WATCHDOG_PID" 2>/dev/null || true
        fi
        WATCHDOG_PID=""
        # Use terraform's actual exit code if meaningful (< 128)
        if [ "$ACTUAL_EXIT" -lt 128 ] 2>/dev/null; then
            EXIT_CODE=$ACTUAL_EXIT
        fi
    fi
    CHILD_PID=""
}

# --- Combined log capture ---
# All script output (setup + init + plan + apply) accumulates into a single
# file that an EXIT trap uploads at the end, regardless of where the script
# exits (early bin-download failure, init failure, plan failure, SIGTERM,
# clean success). This replaces the previous pattern of per-checkpoint
# `curl -sSf … || true` uploads that silently dropped logs whenever the
# script exited before reaching a checkpoint.
COMBINED_LOG="/tmp/combined.log"
: > "$COMBINED_LOG"
log() { echo "$@" | tee -a "$COMBINED_LOG"; }

# --- Configuration ---
TP_BACKEND="${TP_BACKEND:-terraform}"
TP_VERSION="${TP_VERSION:-1.9.8}"
TP_PHASE="${TP_PHASE:-plan}"
# Timeout (seconds) for artifact uploads — logs, plan file, state. These can
# be several MB and the old 10s cap routinely timed out under normal network
# conditions. Small status POSTs keep their tighter local timeouts.
TP_UPLOAD_TIMEOUT="${TP_UPLOAD_TIMEOUT:-60}"
# Artifact name used by upload_log() — set to "apply" when we enter the apply
# phase so the trap writes to /artifacts/apply-log. Default is "plan".
UPLOAD_PHASE="${TP_PHASE:-plan}"
WORK_DIR="/workspace"

mkdir -p "$WORK_DIR"
cd "$WORK_DIR"

# Auth header for all API calls
AUTH_HEADER="Authorization: Bearer $TP_AUTH_TOKEN"

# --- Guaranteed log upload on exit ---
# upload_log and an EXIT trap ensure the combined log is uploaded on every
# exit path. Retries with linear backoff; on final failure, writes a FATAL
# marker to the pod's own stdout/stderr so at minimum the pod log shows the
# upload failed (visible via kubectl logs). Always returns 0 so the trap
# never disturbs the script's real exit code.
upload_log() {
    _artifact="${UPLOAD_PHASE}-log"
    if [ -z "$TP_API_URL" ] || [ -z "$TP_RUN_ID" ] || [ ! -s "$COMBINED_LOG" ]; then
        return 0
    fi
    _i=1
    while [ "$_i" -le 3 ]; do
        if curl -sSf --max-time 30 -X PUT \
             -H "$AUTH_HEADER" \
             -H "Content-Type: application/octet-stream" \
             --data-binary @"$COMBINED_LOG" \
             "${TP_API_URL}/api/terrapod/v1/runs/${TP_RUN_ID}/artifacts/${_artifact}" \
             >/dev/null 2>&1; then
            return 0
        fi
        sleep "$((_i * 2))"
        _i=$((_i + 1))
    done
    _size=$(wc -c < "$COMBINED_LOG" 2>/dev/null || echo 0)
    echo "[entrypoint] FATAL: log upload failed after 3 attempts (artifact=${_artifact}, size=${_size}B, run=${TP_RUN_ID})" >&2
    return 0
}

on_exit() {
    _rc=$?
    upload_log || true
    exit "$_rc"
}
trap on_exit EXIT

# --- Redirect-aware curl wrapper ---
# API endpoints return 302 redirects to presigned URLs. For cloud storage
# (S3, Azure, GCS) the redirect URL is directly reachable. For the filesystem
# storage backend the redirect points at the public hostname (e.g.
# terrapod.local) which may not resolve from inside the cluster. In that case
# we rewrite the URL to use TP_API_URL (the internal service name) so the
# download works.
# Sets TP_LAST_HTTP to the most relevant HTTP status observed. On a
# storage error (e.g. an SSE-KMS bucket rejecting a SigV2 presigned
# URL) the storage response body is logged to stderr so the operator
# sees the real cause instead of a silent failure (#339).
#
# Manually follows redirects (vs `curl -L`) so the apply-phase lock-file
# download against `/api/terrapod/v1/runs/{id}/artifacts/lock-file`
# tolerates a 302 to presigned-URL storage and still surfaces the
# original Authorization-bearing first hop in TP_LAST_HTTP for
# diagnostics (#357). Cloud-storage redirect targets are left
# untouched; same-server hostname rewrites (filesystem backend) are
# normalised back to TP_API_URL.
tp_curl_download() {
    # $1 = output file, remaining args = curl options (URL last)
    _out="$1"; shift
    TP_LAST_HTTP=""
    # First request: don't follow redirects, capture Location header.
    # No `-f` and no `2>/dev/null` — we want the status and any error
    # surfaced, not swallowed.
    if ! _headers=$(curl -sS -D - -o /dev/null "$@"); then
        echo "[entrypoint] download: initial request failed (network/TLS/DNS)" >&2
        return 1
    fi
    _code=$(echo "$_headers" | head -1 | awk '{print $2}')
    TP_LAST_HTTP="$_code"
    case "$_code" in
        301|302|303|307|308)
            _location=$(echo "$_headers" | grep -i '^location:' | sed 's/^[Ll]ocation:[[:space:]]*//' | tr -d '\r')
            if [ -n "$_location" ] && [ -n "$TP_API_URL" ]; then
                # Extract host from redirect URL and from TP_API_URL
                _redir_host=$(echo "$_location" | sed -n 's|^https\{0,1\}://\([^/:]*\).*|\1|p')
                _api_host=$(echo "$TP_API_URL" | sed -n 's|^https\{0,1\}://\([^/:]*\).*|\1|p')
                # Only rewrite if the redirect points at the same logical
                # server but with a different hostname (filesystem backend).
                # Cloud storage URLs (*.amazonaws.com, *.blob.core.windows.net,
                # *.storage.googleapis.com) are left untouched.
                if [ "$_redir_host" != "$_api_host" ]; then
                    _path=$(echo "$_location" | sed 's|^https\{0,1\}://[^/]*||')
                    # Check if path is a filesystem presigned URL
                    # (/api/terrapod/v1/storage/...).
                    case "$_path" in
                        /api/terrapod/v1/storage/*)
                            _location="${TP_API_URL}${_path}"
                            ;;
                    esac
                fi
            fi
            # Follow any further redirects (e.g. S3 region/path-style
            # redirects). NO `-f`: on a 4xx/5xx we want the response
            # body (e.g. the S3 InvalidArgument XML) written out so we
            # can show it, not a bare "curl: (22) ... 400".
            _final=$(curl -sSL -o "$_out" -w '%{http_code}' "$_location" 2>/dev/null || echo 000)
            TP_LAST_HTTP="$_final"
            case "$_final" in
                2*) : ;;  # success
                *)
                    echo "[entrypoint] download: storage returned HTTP $_final for the presigned URL" >&2
                    echo "[entrypoint] storage response body (first 2KB):" >&2
                    head -c 2048 "$_out" 2>/dev/null >&2 || true
                    echo >&2
                    rm -f "$_out"
                    return 1
                    ;;
            esac
            ;;
        200)
            # No redirect — re-fetch with output (rare, but handle it)
            if ! curl -sS -o "$_out" "$@"; then
                echo "[entrypoint] download: direct fetch failed (HTTP $_code)" >&2
                return 1
            fi
            ;;
        *)
            echo "[entrypoint] download: unexpected HTTP $_code from ${*##* }" >&2
            echo "$_headers" | head -1 >&2
            return 1
            ;;
    esac
}

# --- OPA policy evaluation (#343) ---
# Fetches the applicable policy bundle from the API, evaluates each
# policy with `opa eval` against the local plan JSON, and posts the
# results back BEFORE plan-result. The API's post-plan gate then just
# queries the recorded rows — no JSON-wait dance, no in-API OPA.
#
# Uses /tmp/plan.json as input (produced earlier by `tofu show -json
# tfplan`). If the JSON wasn't produced, every applicable set is
# recorded as `errored` (fail-closed for mandatory sets).
#
# Bundle-fetch failure after bounded retries is fatal — refusing to
# proceed is safer than silently skipping the gate.
tp_evaluate_policies() {
    [ -n "$TP_API_URL" ] || return 0
    [ -n "$TP_RUN_ID" ] || return 0

    log "[entrypoint] Fetching policy bundle..."
    _bundle_http=""
    for _attempt in 1 2 3; do
        # --max-time bounds each attempt at TP_UPLOAD_TIMEOUT (default
        # 60s), matching every other curl in this script — otherwise
        # a TCP-stalled connection could burn the full SIGTERM grace
        # budget on three back-to-back hangs.
        _bundle_http=$(curl -sS -o /tmp/policy-bundle.json -w '%{http_code}' \
            --max-time "${TP_UPLOAD_TIMEOUT:-60}" \
            -H "$AUTH_HEADER" \
            "${TP_API_URL}/api/terrapod/v1/runs/${TP_RUN_ID}/policy-bundle" 2>/dev/null || echo 000)
        [ "$_bundle_http" = "200" ] && break
        log "[entrypoint] Policy bundle fetch attempt $_attempt: HTTP $_bundle_http (will retry)"
        sleep 3
    done
    if [ "$_bundle_http" != "200" ] || [ ! -s /tmp/policy-bundle.json ]; then
        log "[entrypoint] FATAL: policy bundle fetch failed (HTTP $_bundle_http)"
        return 1
    fi

    _set_count=$(jq '.policy_sets | length' /tmp/policy-bundle.json 2>/dev/null || echo 0)
    if [ "$_set_count" = "0" ]; then
        log "[entrypoint] No applicable policy sets — skipping evaluation"
        return 0
    fi

    log "[entrypoint] Evaluating $_set_count policy set(s)..."

    # data.terrapod_context — the Terrapod metadata exposed to policies.
    jq '{terrapod_context: .context}' /tmp/policy-bundle.json > /tmp/policy-context.json

    # Build results: one entry per policy set, with per-policy detail.
    _results='[]'
    _idx=0
    while [ "$_idx" -lt "$_set_count" ]; do
        _ps=$(jq ".policy_sets[$_idx]" /tmp/policy-bundle.json)
        _ps_id=$(echo "$_ps" | jq -r '.id')
        _ps_name=$(echo "$_ps" | jq -r '.name')
        _ps_enf=$(echo "$_ps" | jq -r '.enforcement_level')
        _p_count=$(echo "$_ps" | jq '.policies | length')

        _policies_json='[]'
        _set_outcome="passed"
        _p_idx=0
        while [ "$_p_idx" -lt "$_p_count" ]; do
            _p_name=$(echo "$_ps" | jq -r ".policies[$_p_idx].name")
            echo "$_ps" | jq -r ".policies[$_p_idx].rego" > /tmp/policy.rego
            _opa_exit=0

            if [ -s /tmp/plan.json ]; then
                _opa_out=$(opa eval --format json --stdin-input \
                    --data /tmp/policy.rego --data /tmp/policy-context.json \
                    'data.terrapod' < /tmp/plan.json 2>/tmp/opa-err.txt) || _opa_exit=$?
            else
                _opa_exit=1
                _opa_out=""
                echo "plan JSON was not available for policy evaluation" > /tmp/opa-err.txt
            fi

            if [ "$_opa_exit" != "0" ]; then
                _err_msg=$(head -c 1000 /tmp/opa-err.txt | jq -Rs .)
                _policies_json=$(echo "$_policies_json" | jq \
                    --arg name "$_p_name" --argjson err "$_err_msg" \
                    '. + [{policy: $name, passed: false, violations: [], warnings: [], error: $err}]')
                _set_outcome="errored"
            else
                # Defensive jq: handles every shape OPA can serialise the
                # rule values as — a missing query result (no matching
                # rules), a missing `deny`/`warn`, an empty set, an array
                # (the normal partial-set case `deny contains msg if ...`),
                # OR a scalar (a misauthored `deny := true` or
                # `deny := "msg"`). Without the coercion a scalar would
                # error out `.[] | tostring`, the assignment would end up
                # empty, and the policy would silently "pass" despite a
                # would-be denial — defeating the whole gate.
                _deny=$(echo "$_opa_out" | jq '.result // [] | .[0] // {} | .expressions // [] | .[0] // {} | .value // {} | .deny // [] | (if type == "array" then . else [.] end) | map(tostring) | sort')
                _warn=$(echo "$_opa_out" | jq '.result // [] | .[0] // {} | .expressions // [] | .[0] // {} | .value // {} | .warn // [] | (if type == "array" then . else [.] end) | map(tostring) | sort')
                _vlen=$(echo "$_deny" | jq 'length')
                if [ "$_vlen" -gt 0 ] && [ "$_set_outcome" != "errored" ]; then
                    _set_outcome="failed"
                fi
                _passed=$([ "$_vlen" = "0" ] && echo "true" || echo "false")
                _policies_json=$(echo "$_policies_json" | jq \
                    --arg name "$_p_name" --argjson deny "$_deny" \
                    --argjson warn "$_warn" --argjson passed "$_passed" \
                    '. + [{policy: $name, passed: $passed, violations: $deny, warnings: $warn, error: null}]')
            fi
            _p_idx=$((_p_idx + 1))
        done

        _entry=$(jq -n \
            --arg ps_id "$_ps_id" --arg ps_name "$_ps_name" \
            --arg enf "$_ps_enf" --arg outcome "$_set_outcome" \
            --argjson policies "$_policies_json" \
            --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
            '{policy_set_id: $ps_id, policy_set_name: $ps_name, enforcement_level: $enf, outcome: $outcome, result: {policies: $policies, evaluated_at: $ts}}')
        _results=$(echo "$_results" | jq --argjson r "$_entry" '. + [$r]')
        _idx=$((_idx + 1))
    done

    log "[entrypoint] Posting $(echo "$_results" | jq 'length') policy evaluation result(s)"
    # Bounded retries on the POST: the API enforces ON CONFLICT DO NOTHING
    # on (run_id, policy_set_id), so a retried POST after a transient
    # 5xx / network drop is idempotent. Mirrors the bundle-GET retry —
    # the asymmetry round 1 had was needless.
    echo "$_results" | jq '{results: .}' > /tmp/policy-post.body
    _post_http=""
    for _attempt in 1 2 3; do
        # --max-time bounds each attempt at TP_UPLOAD_TIMEOUT (see the
        # bundle GET above for rationale).
        _post_http=$(curl -sS -o /tmp/policy-post.out -w '%{http_code}' \
            --max-time "${TP_UPLOAD_TIMEOUT:-60}" \
            -X POST -H "$AUTH_HEADER" -H "Content-Type: application/json" \
            --data-binary @/tmp/policy-post.body \
            "${TP_API_URL}/api/terrapod/v1/runs/${TP_RUN_ID}/policy-results" 2>/dev/null || echo 000)
        [ "$_post_http" = "201" ] && break
        log "[entrypoint] Policy results POST attempt $_attempt: HTTP $_post_http (will retry)"
        sleep 3
    done
    if [ "$_post_http" != "201" ]; then
        log "[entrypoint] FATAL: policy results POST failed after 3 attempts (HTTP $_post_http): $(head -c 500 /tmp/policy-post.out 2>/dev/null)"
        return 1
    fi
    log "[entrypoint] Policy results recorded"
}

# --- Download binary from cache ---
# Detect platform architecture for correct binary download
TP_OS=$(uname -s | tr '[:upper:]' '[:lower:]')
TP_ARCH=$(uname -m)
case "$TP_ARCH" in
    x86_64)  TP_ARCH="amd64" ;;
    aarch64) TP_ARCH="arm64" ;;
esac

if [ -n "$TP_API_URL" ] && [ -n "$TP_VERSION" ]; then
    BINARY_URL="${TP_API_URL}/api/terrapod/v1/binary-cache/${TP_BACKEND}/${TP_VERSION}/${TP_OS}/${TP_ARCH}"
    log "[entrypoint] Downloading $TP_BACKEND $TP_VERSION ($TP_OS/$TP_ARCH) from binary cache..."
    if ! tp_curl_download "/tmp/${TP_BACKEND}.zip" -H "$AUTH_HEADER" "$BINARY_URL"; then
        log "[entrypoint] Binary cache unavailable, downloading from upstream..."
        # The upstream release artifact only exists for a fully-qualified
        # x.y.z. The API resolves a partial workspace version (e.g.
        # "1.11") to an exact one and pins it on the run, so TP_VERSION
        # should already be exact here. If it isn't (older API, or
        # resolution was unreachable at run creation), fail with an
        # actionable message instead of a bare curl 404 (#338).
        case "$TP_VERSION" in
            [0-9]*.[0-9]*.[0-9]*) : ;;  # x.y.z (optionally -rc/-beta) — OK
            *)
                log "[entrypoint] ERROR: upstream fallback needs a fully-qualified" \
                    "version but got '$TP_VERSION'. The binary cache normally" \
                    "resolves partial versions; a non-exact version here means the" \
                    "cache request failed AND the version was never pinned. Set the" \
                    "workspace version to an exact x.y.z, or fix the runner->API" \
                    "binary-cache path. See terrapod issue #338."
                exit 1
                ;;
        esac
        if [ "$TP_BACKEND" = "terraform" ]; then
            _upstream="https://releases.hashicorp.com/terraform/${TP_VERSION}/terraform_${TP_VERSION}_${TP_OS}_${TP_ARCH}.zip"
        else
            _upstream="https://github.com/opentofu/opentofu/releases/download/v${TP_VERSION}/tofu_${TP_VERSION}_${TP_OS}_${TP_ARCH}.zip"
        fi
        curl -sSfL -o "/tmp/${TP_BACKEND}.zip" "$_upstream"
    fi
else
    log "[entrypoint] No API URL, expecting $TP_BACKEND on PATH"
    TP_BIN="$TP_BACKEND"
fi

if [ -z "$TP_BIN" ]; then
    _zip="/tmp/${TP_BACKEND}.zip"
    # Validate the download is a zip (PK\x03\x04 magic bytes)
    if ! head -c 4 "$_zip" 2>/dev/null | od -A n -t x1 | grep -q '50 4b 03 04'; then
        log "[entrypoint] ERROR: Downloaded file is not a valid zip archive"
        log "[entrypoint] First bytes: $(head -c 64 "$_zip" 2>/dev/null | od -A n -t x1 | head -1)"
        log "[entrypoint] This usually means the presigned storage URL returned an error"
        log "[entrypoint] Check that the API storage backend region/endpoint is correct"
        exit 1
    fi
    unzip -o -q "$_zip" -d /tmp/bin
    chmod +x "/tmp/bin/${TP_BACKEND}"
    TP_BIN="/tmp/bin/${TP_BACKEND}"
fi

# --- Download configuration archive ---
if [ -n "$TP_API_URL" ] && [ -n "$TP_RUN_ID" ]; then
    log "[entrypoint] Downloading configuration..."
    # No `2>/dev/null || true`: a storage auth failure here (e.g. an
    # SSE-KMS bucket rejecting a SigV2 presigned URL) must be visible,
    # not masked into a later misleading "working directory not found"
    # (#339). tp_curl_download surfaces the real status + body itself.
    if ! tp_curl_download /tmp/config.tar.gz -H "$AUTH_HEADER" \
        "${TP_API_URL}/api/terrapod/v1/runs/${TP_RUN_ID}/artifacts/config"; then
        log "[entrypoint] Configuration archive download failed (HTTP ${TP_LAST_HTTP:-unknown}) — see storage error above"
    fi
    if [ -f /tmp/config.tar.gz ] && [ -s /tmp/config.tar.gz ]; then
        # --no-same-owner: don't try to restore original UIDs (we run as non-root)
        # BusyBox tar returns non-zero on harmless utime/chmod warnings for "."
        # entry when running as non-root — suppress and let terraform fail later
        # if extraction actually failed
        tar xzf /tmp/config.tar.gz --no-same-owner --no-same-permissions -C "$WORK_DIR" 2>/dev/null || true

        # Determine the directory containing .tf files (working directory or repo root)
        STRIP_DIR="$WORK_DIR"
        if [ -n "${TP_WORKING_DIR:-}" ] && [ -d "$WORK_DIR/$TP_WORKING_DIR" ]; then
            STRIP_DIR="$WORK_DIR/$TP_WORKING_DIR"
        fi

        # Force the runner to use the local backend by writing a terraform
        # override file (#346). Override files (*_override.tf) are merged
        # by terraform/tofu with replacement semantics: an override
        # `terraform { backend "local" {} }` replaces the main config's
        # `cloud {}` or `backend "x" {}` block, regardless of how the
        # user wrote it. This avoids any in-place editing of user code.
        #
        # The runner MUST execute on the local backend — a remote backend
        # inside the Job recurses straight back into Terrapod. So our
        # override has to WIN, unconditionally. Two mechanisms:
        #
        #   1. Filename `zzzz_terrapod_backend_override.tf`. Override
        #      files are merged in lexical order with the LAST file
        #      winning, so the `zzzz` prefix makes ours win against a
        #      user's `override.tf` or any realistic `*_override.tf`.
        #      (We do NOT defer to a user-supplied backend override —
        #      deferring to a user `cloud {}` / `backend "remote"` would
        #      hand the runner a remote backend. Ours always wins.)
        #
        #   2. The post-init backstop below
        #      (.terraform/terraform.tfstate backend.type check) — a
        #      hard guarantee for the residual case of a user file that
        #      sorts even later than ours.
        #
        # If the user did ship their own override file declaring a
        # backend/cloud block, log it so the override is visible in the
        # runner log — but still write (and win with) ours.
        #
        # NB: we DO keep the user's committed `.terraform.lock.hcl` if
        # present. It pins provider versions across plan/apply (see #306);
        # discarding it makes plan-init and apply-init independent
        # resolutions of the version constraint, which can drift.
        TP_OVERRIDE_FILE="$STRIP_DIR/zzzz_terrapod_backend_override.tf"
        for tp_ov in "$STRIP_DIR"/override.tf "$STRIP_DIR"/*_override.tf; do
            [ -f "$tp_ov" ] || continue
            case "$tp_ov" in
                "$TP_OVERRIDE_FILE") continue ;;
            esac
            if grep -qE '^[[:space:]]*terraform[[:space:]]*\{' "$tp_ov" \
               && grep -qE '(^|[[:space:]])(backend[[:space:]]*"|cloud[[:space:]]*\{)' "$tp_ov"; then
                log "[entrypoint] Note: user override $tp_ov declares a backend/cloud block — Terrapod's local-backend override takes precedence"
            fi
        done
        cat > "$TP_OVERRIDE_FILE" <<'TPOVR'
# Terrapod runner: force local backend for in-runner execution.
# Override files (*_override.tf) are merged by terraform/tofu with
# replacement semantics over the main config — this displaces any
# `cloud {}` or `backend "x" {}` declared in the main config. The
# `zzzz` prefix makes this file sort last so it wins the override merge.
terraform {
  backend "local" {}
}
TPOVR
        log "[entrypoint] Wrote $TP_OVERRIDE_FILE (forces local backend via override)"
    else
        log "[entrypoint] No configuration archive (HTTP ${TP_LAST_HTTP:-none})"
    fi
fi

# --- Run setup script (if configured) ---
if [ -n "$TP_SETUP_SCRIPT" ]; then
    log "[entrypoint] Running setup script..."
    eval "$TP_SETUP_SCRIPT"
fi

# --- Configure provider mirror + credentials ---
# Only configure network mirror for HTTPS URLs (terraform/tofu require HTTPS)
if [ -n "$TP_API_URL" ]; then
    # Extract hostname from API URL for credentials block
    MIRROR_HOST=$(echo "$TP_API_URL" | sed -n 's|^https\{0,1\}://\([^/:]*\).*|\1|p')

    case "$TP_API_URL" in
        https://*)
            cat > /tmp/terraform.rc <<TFEOF
credentials "$MIRROR_HOST" {
  token = "$TP_AUTH_TOKEN"
}
provider_installation {
  network_mirror {
    url = "${TP_API_URL}/v1/providers/"
    exclude = ["${MIRROR_HOST}/*/*"]
  }
  direct {
    include = ["${MIRROR_HOST}/*/*"]
  }
}
TFEOF
            export TF_CLI_CONFIG_FILE="/tmp/terraform.rc"
            log "[entrypoint] Provider mirror + credentials configured: ${TP_API_URL}/v1/providers/"
            ;;
        *)
            # HTTP — skip network mirror (terraform requires HTTPS) but still
            # write credentials for private registry access
            cat > /tmp/terraform.rc <<TFEOF
credentials "$MIRROR_HOST" {
  token = "$TP_AUTH_TOKEN"
}
TFEOF
            export TF_CLI_CONFIG_FILE="/tmp/terraform.rc"
            log "[entrypoint] Skipping provider mirror (requires HTTPS), credentials configured for: $MIRROR_HOST"
            ;;
    esac
fi

# --- Public hostname redirect (split-networking deployments) ---
# When the deployment uses a separate internal API URL for runners (see
# the internalIngress + listener.publicApiUrl pattern in the Terrapod
# Helm chart), the runner's TP_API_URL points at the internal hostname
# while user code under `source = "..."` references the public/canonical
# hostname. Add a terraform CLI `host{}` block redirecting public→
# internal so module + provider registry discovery for the canonical
# hostname resolves via the internal route.
#
# Credentials are also written for the public host because terraform
# matches credentials by the source-URL hostname, not the discovery
# target. Without this the host{} redirect would resolve services but
# terraform would still fail auth ("no credentials block for host X").
#
# Service URLs match what /.well-known/terraform.json advertises in
# api/routers/oauth.py — modules.v1 = /api/v2/registry/modules/,
# providers.v1 = /api/v2/registry/providers/ (the *registry* protocol;
# /v1/providers/ is the separate network-mirror protocol).
#
# Port suffixes (e.g. host:8443) are stripped by the host extraction
# regex below, so a public URL and internal URL that differ only by
# port are treated as the same host — no redirect emitted. Acceptable:
# terraform's host discovery is hostname-keyed, not host+port keyed.
#
# HTTP fallback note: when TP_API_URL is HTTP (no provider mirror in
# the existing terraform.rc), the redirect still works — host{} only
# affects service discovery. Provider DOWNLOAD via terraform's default
# direct mode requires HTTPS, so an HTTP-only deployment can't serve
# providers via this redirect; that's a pre-existing limitation.
INTERNAL_HOST=$(echo "${TP_API_URL:-}" | sed -n 's|^https\{0,1\}://\([^/:]*\).*|\1|p')
if [ -n "$TP_PUBLIC_API_URL" ] && [ -n "$TF_CLI_CONFIG_FILE" ] && [ -n "$INTERNAL_HOST" ]; then
    PUBLIC_HOST=$(echo "$TP_PUBLIC_API_URL" | sed -n 's|^https\{0,1\}://\([^/:]*\).*|\1|p')
    if [ -n "$PUBLIC_HOST" ] && [ "$PUBLIC_HOST" != "$INTERNAL_HOST" ]; then
        cat >> "$TF_CLI_CONFIG_FILE" <<TFEOF
credentials "$PUBLIC_HOST" {
  token = "$TP_AUTH_TOKEN"
}
host "$PUBLIC_HOST" {
  services = {
    "modules.v1"   = "${TP_API_URL}/api/v2/registry/modules/"
    "providers.v1" = "${TP_API_URL}/api/v2/registry/providers/"
  }
}
TFEOF
        log "[entrypoint] Configured host{} redirect: $PUBLIC_HOST → $TP_API_URL"
    fi
fi

# --- Provider download timeouts ---
# Increase registry client timeout (default 10s) and enable retries for
# provider binary downloads. Covers first-request latency when the provider
# mirror is caching a binary on-demand from upstream.
export TF_REGISTRY_CLIENT_TIMEOUT=30
export TF_PROVIDER_DOWNLOAD_RETRY=3

# --- Change to working directory (monorepo subdirectory support) ---
if [ -n "${TP_WORKING_DIR:-}" ]; then
    # Sanitize: strip leading/trailing slashes, reject path traversal
    TP_WORKING_DIR=$(echo "$TP_WORKING_DIR" | sed 's|^/*||;s|/*$||')
    case "$TP_WORKING_DIR" in
        *..*)
            log "[entrypoint] ERROR: working directory contains path traversal"
            exit 1
            ;;
    esac
    TARGET_DIR="$WORK_DIR/$TP_WORKING_DIR"
    if [ ! -d "$TARGET_DIR" ]; then
        log "[entrypoint] ERROR: working directory '$TP_WORKING_DIR' not found in config"
        exit 1
    fi
    cd "$TARGET_DIR"
    log "[entrypoint] Changed to working directory: $TP_WORKING_DIR"
fi

# --- Download current state ---
# Must run AFTER working directory change so terraform.tfstate ends up in the
# directory where tofu init/plan/apply will execute (the working directory for
# monorepo subdirectory setups, or $WORK_DIR for root-level workspaces).
if [ -n "$TP_API_URL" ] && [ -n "$TP_RUN_ID" ]; then
    log "[entrypoint] Downloading current state..."
    tp_curl_download terraform.tfstate -H "$AUTH_HEADER" \
        "${TP_API_URL}/api/terrapod/v1/runs/${TP_RUN_ID}/artifacts/state" 2>/dev/null || true
fi

# --- Apply phase: try to reuse the plan-phase lock file ---
# Carrying .terraform.lock.hcl from plan to apply forces both inits to
# resolve to the same provider versions — without this, the apply-phase
# init re-evaluates the version constraint and may pick up a newer
# matching version published in the plan→apply window (see #306).
#
# Best-effort: 404/network failures here just warn. The apply still
# works (with the today-behaviour drift risk) if the plan ran on an
# older runner that didn't upload a lock file, or an older API without
# the /lock-file endpoint.
if [ "$TP_PHASE" = "apply" ] && [ -n "$TP_API_URL" ] && [ -n "$TP_RUN_ID" ]; then
    if tp_curl_download .terraform.lock.hcl -H "$AUTH_HEADER" \
        "${TP_API_URL}/api/terrapod/v1/runs/${TP_RUN_ID}/artifacts/lock-file" 2>/dev/null; then
        log "[entrypoint] Reusing .terraform.lock.hcl from plan phase"
    else
        rm -f .terraform.lock.hcl
        log "[entrypoint] No plan-phase lock file available (HTTP ${TP_LAST_HTTP:-none}); apply init will resolve providers independently"
    fi
fi

# --- Initialize ---
log "[entrypoint] Running $TP_BACKEND init..."
INIT_EXIT=0
"$TP_BIN" init -input=false > /tmp/init.log 2>&1 || INIT_EXIT=$?
cat /tmp/init.log
cat /tmp/init.log >> "$COMBINED_LOG"
if [ "$INIT_EXIT" != "0" ]; then
    log "[entrypoint] Init failed with exit code $INIT_EXIT"
    # Log uploaded by on_exit trap — no explicit upload here.
    exit "$INIT_EXIT"
fi

# --- Backend backstop (#346) ---
# The runner MUST execute on the local backend; a remote backend inside
# the Job recurses straight back into Terrapod. The
# zzzz_terrapod_backend_override.tf override file forces this by winning
# the override-file merge. As a hard guarantee against a pathological
# user file that sorts even later, verify the backend that init actually
# configured — it is recorded in .terraform/terraform.tfstate.
TP_CONFIGURED_BACKEND=$(jq -r '.backend.type // "MISSING"' .terraform/terraform.tfstate 2>/dev/null || echo "MISSING")
# jq on an empty file exits 0 with empty output — normalise to MISSING so
# the diagnostic below reads sensibly (the != local check fails safe
# either way).
[ -n "$TP_CONFIGURED_BACKEND" ] || TP_CONFIGURED_BACKEND="MISSING"
if [ "$TP_CONFIGURED_BACKEND" != "local" ]; then
    log "[entrypoint] FATAL: expected the local backend after init, got '$TP_CONFIGURED_BACKEND'."
    log "[entrypoint] A user-supplied override file appears to have displaced the Terrapod backend override."
    log "[entrypoint] Remove any committed override.tf / *_override.tf that declares a 'backend' or 'cloud' block."
    exit 1
fi
log "[entrypoint] Backend verified: local"

# --- Plan phase: upload the lock file produced (or augmented) by init ---
# Apply phase will download this so its init resolves to the same
# provider versions. Best-effort — a failure here just means the
# apply phase falls back to today's behaviour.
if [ "$TP_PHASE" = "plan" ] && [ -n "$TP_API_URL" ] && [ -n "$TP_RUN_ID" ] && [ -f .terraform.lock.hcl ]; then
    LOCK_UP_HTTP=$(curl -sS -o /dev/null -w "%{http_code}" \
        -X PUT -H "$AUTH_HEADER" --max-time "${TP_UPLOAD_TIMEOUT:-60}" \
        --data-binary @.terraform.lock.hcl \
        "${TP_API_URL}/api/terrapod/v1/runs/${TP_RUN_ID}/artifacts/lock-file" 2>/dev/null || echo "000")
    if [ "$LOCK_UP_HTTP" = "204" ] || [ "$LOCK_UP_HTTP" = "200" ]; then
        log "[entrypoint] Uploaded .terraform.lock.hcl for apply-phase reuse"
    else
        log "[entrypoint] Lock file upload returned HTTP $LOCK_UP_HTTP (non-fatal); apply phase will resolve providers independently"
    fi
fi

# --- Build -var-file arguments from TP_VAR_FILES JSON ---
# Uses a temp file + line-by-line read to safely handle paths with spaces.
# The API validates paths at ingestion (no traversal, no shell metacharacters),
# but we still quote properly here for defense in depth.
set --
if [ -n "$TP_VAR_FILES" ] && [ "$TP_VAR_FILES" != "[]" ]; then
    echo "$TP_VAR_FILES" | jq -r '.[]' > /tmp/var_files.txt
    while IFS= read -r vf; do
        set -- "$@" "-var-file=$vf"
    done < /tmp/var_files.txt
    rm -f /tmp/var_files.txt
    log "[entrypoint] Using var files: $TP_VAR_FILES"
fi

# --- Build -target arguments from TP_TARGET_ADDRS JSON ---
if [ -n "$TP_TARGET_ADDRS" ] && [ "$TP_TARGET_ADDRS" != "[]" ]; then
    echo "$TP_TARGET_ADDRS" | jq -r '.[]' > /tmp/targets.txt
    while IFS= read -r tgt; do
        set -- "$@" "-target=$tgt"
    done < /tmp/targets.txt
    rm -f /tmp/targets.txt
    log "[entrypoint] Using targets: $TP_TARGET_ADDRS"
fi

# --- Build -replace arguments from TP_REPLACE_ADDRS JSON (plan phase only) ---
if [ "$TP_PHASE" = "plan" ] && [ -n "$TP_REPLACE_ADDRS" ] && [ "$TP_REPLACE_ADDRS" != "[]" ]; then
    echo "$TP_REPLACE_ADDRS" | jq -r '.[]' > /tmp/replaces.txt
    while IFS= read -r rpl; do
        set -- "$@" "-replace=$rpl"
    done < /tmp/replaces.txt
    rm -f /tmp/replaces.txt
    log "[entrypoint] Using replace addrs: $TP_REPLACE_ADDRS"
fi

# --- Execute phase ---
EXIT_CODE=0

if [ "$TP_PHASE" = "plan" ]; then
    echo "[entrypoint] Running $TP_BACKEND plan..."
    # Redirect to file so $! gives the plan PID for correct signal forwarding.
    # A background tail -f streams output to pod logs in real-time.
    PLAN_ARGS="-input=false -detailed-exitcode"
    # Always write the binary plan file. For non-plan-only runs the apply
    # phase consumes it; for plan-only runs we still need it as input to
    # `tofu show -json` for the json-output artifact.
    PLAN_ARGS="$PLAN_ARGS -out=tfplan"
    if [ "${TP_REFRESH_ONLY:-false}" = "true" ]; then
        PLAN_ARGS="$PLAN_ARGS -refresh-only"
    fi
    if [ "${TP_REFRESH:-true}" = "false" ]; then
        PLAN_ARGS="$PLAN_ARGS -refresh=false"
    fi
    if [ "${TP_DESTROY:-false}" = "true" ]; then
        PLAN_ARGS="$PLAN_ARGS -destroy"
    fi
    : > /tmp/plan.log
    "$TP_BIN" plan $PLAN_ARGS "$@" > /tmp/plan.log 2>&1 &
    CHILD_PID=$!
    # Stream log to pod stdout in real-time so the listener can capture it
    # for live log display in the UI. The file redirect preserves $! as
    # the tofu PID for correct signal forwarding.
    tail -f /tmp/plan.log &
    TAIL_PID=$!
    wait_for_child
    kill "$TAIL_PID" 2>/dev/null; wait "$TAIL_PID" 2>/dev/null || true

    # -detailed-exitcode: 0=no changes, 1=error, 2=changes present
    PLAN_HAS_CHANGES="false"
    if [ "$EXIT_CODE" = "2" ]; then
        PLAN_HAS_CHANGES="true"
        echo "[entrypoint] PLAN_HAS_CHANGES=true"
        EXIT_CODE=0
    elif [ "$EXIT_CODE" = "0" ]; then
        echo "[entrypoint] PLAN_HAS_CHANGES=false"
    fi

    # Emit the structured JSON plan ahead of plan-result (#343 OPA-on-runner).
    # OPA policy evaluation runs against /tmp/plan.json; the same file is
    # uploaded as the plan-json-output artifact for the UI. `show -json` is
    # best-effort: failure is non-fatal here, but the policy gate will
    # then record fail-closed `errored` outcomes for every applicable set.
    # The presence of `tfplan` is the right gate: -detailed-exitcode 1
    # (errored) doesn't produce one; both 0 (no changes) and 2 (changes) do.
    if [ "$EXIT_CODE" = "0" ] && [ -f tfplan ]; then
        if ! "$TP_BIN" show -json tfplan > /tmp/plan.json 2> /tmp/plan-show.err; then
            log "[entrypoint] $TP_BIN show -json tfplan failed (non-fatal): $(head -c 500 /tmp/plan-show.err)"
            rm -f /tmp/plan.json
        fi
    fi

    # OPA policy evaluation (#343). Runs ONLY when the plan succeeded —
    # an errored plan has no JSON to evaluate against, and the API gate
    # never sees that run anyway (plan-result isn't posted below).
    # Fail-closed for mandatory sets if the bundle fetch or results POST
    # can't be completed.
    if [ "$EXIT_CODE" = "0" ]; then
        if ! tp_evaluate_policies; then
            exit 1
        fi
    fi

    # Report has_changes to API (used by reconciler for drift detection).
    # The API's post-plan policy gate now runs against the rows the
    # runner posted in tp_evaluate_policies above, so by the time
    # complete_plan handles this plan-result the gate state is settled.
    if [ -n "$TP_API_URL" ] && [ -n "$TP_RUN_ID" ] && [ "$EXIT_CODE" = "0" ]; then
        HAS_CHANGES_JSON="$PLAN_HAS_CHANGES"
        curl -sSf --max-time 10 -X POST -H "$AUTH_HEADER" \
            -H "Content-Type: application/json" \
            -d "{\"has_changes\": $HAS_CHANGES_JSON}" \
            "${TP_API_URL}/api/terrapod/v1/runs/${TP_RUN_ID}/plan-result" || true
    fi

    # Append plan.log to combined; on_exit trap uploads the combined log.
    [ -f /tmp/plan.log ] && cat /tmp/plan.log >> "$COMBINED_LOG"

    # Upload plan file (best-effort) — separate artifact, not a log.
    # Skip for plan-only runs: there is no apply phase that would consume
    # the binary, so don't burn storage on it.
    if [ -n "$TP_API_URL" ] && [ -f tfplan ] && [ "${TP_PLAN_ONLY:-false}" != "true" ]; then
        curl -sSf --max-time "$TP_UPLOAD_TIMEOUT" -X PUT -H "$AUTH_HEADER" \
            -H "Content-Type: application/octet-stream" \
            --data-binary @tfplan \
            "${TP_API_URL}/api/terrapod/v1/runs/${TP_RUN_ID}/artifacts/plan-file" || true
    fi

    # Upload structured JSON plan (already produced above for OPA).
    # Best-effort: a failed upload here MUST NOT fail the run — the
    # read endpoint just returns 404.
    if [ -n "$TP_API_URL" ] && [ -s /tmp/plan.json ]; then
        curl -sSf --max-time "$TP_UPLOAD_TIMEOUT" -X PUT -H "$AUTH_HEADER" \
            -H "Content-Type: application/json" \
            --data-binary @/tmp/plan.json \
            "${TP_API_URL}/api/terrapod/v1/runs/${TP_RUN_ID}/artifacts/plan-json-output" \
            || log "[entrypoint] plan-json-output upload failed (non-fatal)"
    fi

elif [ "$TP_PHASE" = "apply" ]; then
    # From here on, the trap uploads to /artifacts/apply-log rather than plan-log.
    UPLOAD_PHASE="apply"

    # Download plan file from plan phase
    if [ -n "$TP_API_URL" ] && [ -n "$TP_RUN_ID" ]; then
        log "[entrypoint] Downloading plan file from plan phase..."
        tp_curl_download tfplan -H "$AUTH_HEADER" \
            "${TP_API_URL}/api/terrapod/v1/runs/${TP_RUN_ID}/artifacts/plan-file" 2>/dev/null || true
    fi

    echo "[entrypoint] Running $TP_BACKEND apply..."
    : > /tmp/apply.log
    if [ -f tfplan ]; then
        # Plan file already includes var-file inputs — no need to re-specify
        "$TP_BIN" apply -input=false tfplan > /tmp/apply.log 2>&1 &
    else
        "$TP_BIN" apply -input=false -auto-approve "$@" > /tmp/apply.log 2>&1 &
    fi
    CHILD_PID=$!
    # Stream log to pod stdout in real-time for live log display in the UI
    tail -f /tmp/apply.log &
    TAIL_PID=$!
    wait_for_child
    kill "$TAIL_PID" 2>/dev/null; wait "$TAIL_PID" 2>/dev/null || true

    # Append apply.log to combined; on_exit trap uploads the combined log.
    [ -f /tmp/apply.log ] && cat /tmp/apply.log >> "$COMBINED_LOG"

    # Upload new state (FATAL — missing state = infrastructure/state divergence)
    if [ -n "$TP_API_URL" ] && [ -f terraform.tfstate ]; then
        if ! curl -sSf --max-time "$TP_UPLOAD_TIMEOUT" -X PUT -H "$AUTH_HEADER" \
            -H "Content-Type: application/octet-stream" \
            --data-binary @terraform.tfstate \
            "${TP_API_URL}/api/terrapod/v1/runs/${TP_RUN_ID}/artifacts/state"; then
            echo "[entrypoint] FATAL: state upload failed — flagging workspace"
            curl -sS --max-time 5 -X POST -H "$AUTH_HEADER" \
                "${TP_API_URL}/api/terrapod/v1/runs/${TP_RUN_ID}/state-diverged" || true
            EXIT_CODE=1
        fi
    fi

    # Report apply completion to API on success — drives the
    # `applying → applied` transition without waiting for the listener-
    # driven Job-status round-trip. Best-effort; the reconciler's listener
    # path is the fallback.
    if [ -n "$TP_API_URL" ] && [ -n "$TP_RUN_ID" ] && [ "$EXIT_CODE" = "0" ]; then
        curl -sSf --max-time 10 -X POST -H "$AUTH_HEADER" \
            "${TP_API_URL}/api/terrapod/v1/runs/${TP_RUN_ID}/apply-result" || true
    fi
fi

echo "[entrypoint] Phase $TP_PHASE completed with exit code $EXIT_CODE"
exit $EXIT_CODE
