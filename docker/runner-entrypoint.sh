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

# --- Setup log capture ---
# All output before terraform/tofu execution is captured so it can be
# included in the uploaded log artifact (visible in the UI after the
# live pod log stream ends).
SETUP_LOG="/tmp/setup.log"
: > "$SETUP_LOG"
log() { echo "$@" | tee -a "$SETUP_LOG"; }

# --- Configuration ---
TP_BACKEND="${TP_BACKEND:-terraform}"
TP_VERSION="${TP_VERSION:-1.9.8}"
TP_PHASE="${TP_PHASE:-plan}"
WORK_DIR="/workspace"

mkdir -p "$WORK_DIR"
cd "$WORK_DIR"

# Auth header for all API calls
AUTH_HEADER="Authorization: Bearer $TP_AUTH_TOKEN"

# --- Redirect-aware curl wrapper ---
# API endpoints return 302 redirects to presigned URLs. For cloud storage
# (S3, Azure, GCS) the redirect URL is directly reachable. For the filesystem
# storage backend the redirect points at the public hostname (e.g.
# terrapod.local) which may not resolve from inside the cluster. In that case
# we rewrite the URL to use TP_API_URL (the internal service name) so the
# download works.
tp_curl_download() {
    # $1 = output file, remaining args = curl options (URL last)
    _out="$1"; shift
    # First request: don't follow redirects, capture Location header
    _headers=$(curl -sSf -D - -o /dev/null "$@" 2>/dev/null)
    _code=$(echo "$_headers" | head -1 | awk '{print $2}')
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
                    # Check if path is a filesystem presigned URL (/api/v2/storage/...)
                    case "$_path" in
                        /api/v2/storage/*)
                            _location="${TP_API_URL}${_path}"
                            ;;
                    esac
                fi
            fi
            # Follow any further redirects (e.g. S3 region/path-style redirects)
            curl -sSfL -o "$_out" "$_location"
            ;;
        200)
            # No redirect — re-fetch with output (rare, but handle it)
            curl -sSf -o "$_out" "$@"
            ;;
        *)
            echo "[entrypoint] Unexpected HTTP $_code" >&2
            return 1
            ;;
    esac
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
    BINARY_URL="${TP_API_URL}/api/v2/binary-cache/${TP_BACKEND}/${TP_VERSION}/${TP_OS}/${TP_ARCH}"
    log "[entrypoint] Downloading $TP_BACKEND $TP_VERSION ($TP_OS/$TP_ARCH) from binary cache..."
    if ! tp_curl_download "/tmp/${TP_BACKEND}.zip" -H "$AUTH_HEADER" "$BINARY_URL"; then
        log "[entrypoint] Binary cache unavailable, downloading from upstream..."
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
    tp_curl_download /tmp/config.tar.gz -H "$AUTH_HEADER" \
        "${TP_API_URL}/api/v2/runs/${TP_RUN_ID}/artifacts/config" 2>/dev/null || true
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

        # Strip cloud {} and backend {} blocks from .tf files.
        # Uploaded configs have cloud/backend blocks that would cause recursive
        # backend use when running in remote execution mode.
        for tf_file in "$STRIP_DIR"/*.tf; do
            [ -f "$tf_file" ] || continue
            awk '
            /^[[:space:]]*(cloud|backend)[[:space:]]*(\{|"[^"]*"[[:space:]]*\{)/ { depth=1; next }
            depth > 0 { if (/\{/) depth++; if (/\}/) depth--; next }
            { print }
            ' "$tf_file" > "${tf_file}.tmp" && mv "${tf_file}.tmp" "$tf_file"
        done
        # Remove lock file — the runner resolves providers independently
        rm -f "$STRIP_DIR/.terraform.lock.hcl"
        log "[entrypoint] Stripped cloud/backend blocks from uploaded config"
    else
        log "[entrypoint] No configuration archive (HTTP $HTTP_CODE)"
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
        "${TP_API_URL}/api/v2/runs/${TP_RUN_ID}/artifacts/state" 2>/dev/null || true
fi

# --- Initialize ---
log "[entrypoint] Running $TP_BACKEND init..."
INIT_EXIT=0
"$TP_BIN" init -input=false > /tmp/init.log 2>&1 || INIT_EXIT=$?
cat /tmp/init.log
if [ "$INIT_EXIT" != "0" ]; then
    log "[entrypoint] Init failed with exit code $INIT_EXIT"
    # Upload setup + init output as plan log so it's visible in the UI
    if [ -n "$TP_API_URL" ] && [ -n "$TP_RUN_ID" ]; then
        cat "$SETUP_LOG" /tmp/init.log > /tmp/plan-full.log 2>/dev/null
        curl -sSf --max-time 10 -X PUT -H "$AUTH_HEADER" \
            -H "Content-Type: application/octet-stream" \
            --data-binary @/tmp/plan-full.log \
            "${TP_API_URL}/api/v2/runs/${TP_RUN_ID}/artifacts/plan-log" || true
    fi
    exit "$INIT_EXIT"
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
    # Only save plan file if not plan-only (plan file is discarded for plan-only runs)
    if [ "${TP_PLAN_ONLY:-false}" != "true" ]; then
        PLAN_ARGS="$PLAN_ARGS -out=tfplan"
    fi
    if [ "${TP_REFRESH_ONLY:-false}" = "true" ]; then
        PLAN_ARGS="$PLAN_ARGS -refresh-only"
    fi
    if [ "${TP_REFRESH:-true}" = "false" ]; then
        PLAN_ARGS="$PLAN_ARGS -refresh=false"
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

    # Report has_changes to API (used by reconciler for drift detection)
    if [ -n "$TP_API_URL" ] && [ -n "$TP_RUN_ID" ] && [ "$EXIT_CODE" = "0" ]; then
        HAS_CHANGES_JSON="$PLAN_HAS_CHANGES"
        curl -sSf --max-time 10 -X POST -H "$AUTH_HEADER" \
            -H "Content-Type: application/json" \
            -d "{\"has_changes\": $HAS_CHANGES_JSON}" \
            "${TP_API_URL}/api/v2/runs/${TP_RUN_ID}/plan-result" || true
    fi

    # Upload plan log (best-effort) — prepend setup + init output so the full log is visible
    if [ -n "$TP_API_URL" ] && [ -f /tmp/plan.log ]; then
        cat "$SETUP_LOG" /tmp/init.log /tmp/plan.log > /tmp/plan-full.log 2>/dev/null
        curl -sSf --max-time 10 -X PUT -H "$AUTH_HEADER" \
            -H "Content-Type: application/octet-stream" \
            --data-binary @/tmp/plan-full.log \
            "${TP_API_URL}/api/v2/runs/${TP_RUN_ID}/artifacts/plan-log" || true
    fi

    # Upload plan file (best-effort)
    if [ -n "$TP_API_URL" ] && [ -f tfplan ]; then
        curl -sSf --max-time 10 -X PUT -H "$AUTH_HEADER" \
            -H "Content-Type: application/octet-stream" \
            --data-binary @tfplan \
            "${TP_API_URL}/api/v2/runs/${TP_RUN_ID}/artifacts/plan-file" || true
    fi

elif [ "$TP_PHASE" = "apply" ]; then
    # Download plan file from plan phase
    if [ -n "$TP_API_URL" ] && [ -n "$TP_RUN_ID" ]; then
        log "[entrypoint] Downloading plan file from plan phase..."
        tp_curl_download tfplan -H "$AUTH_HEADER" \
            "${TP_API_URL}/api/v2/runs/${TP_RUN_ID}/artifacts/plan-file" 2>/dev/null || true
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

    # Upload apply log (best-effort, bounded by --max-time) — prepend setup + init output
    if [ -n "$TP_API_URL" ] && [ -f /tmp/apply.log ]; then
        cat "$SETUP_LOG" /tmp/init.log /tmp/apply.log > /tmp/apply-full.log 2>/dev/null
        curl -sSf --max-time 10 -X PUT -H "$AUTH_HEADER" \
            -H "Content-Type: application/octet-stream" \
            --data-binary @/tmp/apply-full.log \
            "${TP_API_URL}/api/v2/runs/${TP_RUN_ID}/artifacts/apply-log" || \
            echo "[entrypoint] WARNING: apply log upload failed"
    fi

    # Upload new state (FATAL — missing state = infrastructure/state divergence)
    if [ -n "$TP_API_URL" ] && [ -f terraform.tfstate ]; then
        if ! curl -sSf --max-time 15 -X PUT -H "$AUTH_HEADER" \
            -H "Content-Type: application/octet-stream" \
            --data-binary @terraform.tfstate \
            "${TP_API_URL}/api/v2/runs/${TP_RUN_ID}/artifacts/state"; then
            echo "[entrypoint] FATAL: state upload failed — flagging workspace"
            curl -sS --max-time 5 -X POST -H "$AUTH_HEADER" \
                "${TP_API_URL}/api/v2/runs/${TP_RUN_ID}/state-diverged" || true
            EXIT_CODE=1
        fi
    fi
fi

echo "[entrypoint] Phase $TP_PHASE completed with exit code $EXIT_CODE"
exit $EXIT_CODE
