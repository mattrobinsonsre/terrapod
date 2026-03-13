#!/bin/sh
set -e

# Signal-forwarding entrypoint for Terrapod runner Jobs.
#
# Traps SIGTERM/SIGQUIT and forwards them to the terraform/tofu child process
# so it can release state locks and exit cleanly. This is critical for spot
# instance preemption — K8s sends SIGTERM, and we have 120s
# (terminationGracePeriodSeconds) before SIGKILL.
#
# All API calls use TP_AUTH_TOKEN (short-lived runner token from K8s Secret).

CHILD_PID=""

forward_signal() {
    if [ -n "$CHILD_PID" ]; then
        echo "[entrypoint] Received signal, forwarding to child PID $CHILD_PID"
        kill -TERM "$CHILD_PID" 2>/dev/null || true
    fi
}

trap forward_signal TERM QUIT

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
            curl -sSf -o "$_out" "$_location"
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
    echo "[entrypoint] Downloading $TP_BACKEND $TP_VERSION ($TP_OS/$TP_ARCH) from binary cache..."
    tp_curl_download "/tmp/${TP_BACKEND}.zip" -H "$AUTH_HEADER" "$BINARY_URL"
else
    echo "[entrypoint] No API URL, expecting $TP_BACKEND on PATH"
    TP_BIN="$TP_BACKEND"
fi

if [ -z "$TP_BIN" ]; then
    unzip -o -q "/tmp/${TP_BACKEND}.zip" -d /tmp/bin
    chmod +x "/tmp/bin/${TP_BACKEND}"
    TP_BIN="/tmp/bin/${TP_BACKEND}"
fi

# --- Download configuration archive ---
if [ -n "$TP_API_URL" ] && [ -n "$TP_RUN_ID" ]; then
    echo "[entrypoint] Downloading configuration..."
    tp_curl_download /tmp/config.tar.gz -H "$AUTH_HEADER" \
        "${TP_API_URL}/api/v2/runs/${TP_RUN_ID}/artifacts/config" 2>/dev/null || true
    if [ -f /tmp/config.tar.gz ] && [ -s /tmp/config.tar.gz ]; then
        # --no-same-owner: don't try to restore original UIDs (we run as non-root)
        # BusyBox tar returns non-zero on harmless utime/chmod warnings for "."
        # entry when running as non-root — suppress and let terraform fail later
        # if extraction actually failed
        tar xzf /tmp/config.tar.gz --no-same-owner --no-same-permissions -C "$WORK_DIR" 2>/dev/null || true

        # Strip cloud {} and backend {} blocks from .tf files.
        # Uploaded configs have cloud/backend blocks that would cause recursive
        # backend use when running in remote execution mode.
        for tf_file in "$WORK_DIR"/*.tf; do
            [ -f "$tf_file" ] || continue
            awk '
            /^[[:space:]]*(cloud|backend)[[:space:]]*(\{|"[^"]*"[[:space:]]*\{)/ { depth=1; next }
            depth > 0 { if (/\{/) depth++; if (/\}/) depth--; next }
            { print }
            ' "$tf_file" > "${tf_file}.tmp" && mv "${tf_file}.tmp" "$tf_file"
        done
        # Remove lock file — the runner resolves providers independently
        rm -f "$WORK_DIR/.terraform.lock.hcl"
        echo "[entrypoint] Stripped cloud/backend blocks from uploaded config"
    else
        echo "[entrypoint] No configuration archive (HTTP $HTTP_CODE)"
    fi
fi

# --- Download current state ---
if [ -n "$TP_API_URL" ] && [ -n "$TP_RUN_ID" ]; then
    echo "[entrypoint] Downloading current state..."
    tp_curl_download "$WORK_DIR/terraform.tfstate" -H "$AUTH_HEADER" \
        "${TP_API_URL}/api/v2/runs/${TP_RUN_ID}/artifacts/state" 2>/dev/null || true
fi

# --- Run setup script (if configured) ---
if [ -n "$TP_SETUP_SCRIPT" ]; then
    echo "[entrypoint] Running setup script..."
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
            echo "[entrypoint] Provider mirror + credentials configured: ${TP_API_URL}/v1/providers/"
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
            echo "[entrypoint] Skipping provider mirror (requires HTTPS), credentials configured for: $MIRROR_HOST"
            ;;
    esac
fi

# --- Initialize ---
echo "[entrypoint] Running $TP_BACKEND init..."
INIT_EXIT=0
"$TP_BIN" init -input=false > /tmp/init.log 2>&1 || INIT_EXIT=$?
cat /tmp/init.log
if [ "$INIT_EXIT" != "0" ]; then
    echo "[entrypoint] Init failed with exit code $INIT_EXIT"
    # Upload init output as plan log so it's visible in the UI
    if [ -n "$TP_API_URL" ] && [ -n "$TP_RUN_ID" ]; then
        curl -sSf -X PUT -H "$AUTH_HEADER" \
            -H "Content-Type: application/octet-stream" \
            --data-binary @/tmp/init.log \
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
    echo "[entrypoint] Using var files: $TP_VAR_FILES"
fi

# --- Build -target arguments from TP_TARGET_ADDRS JSON ---
if [ -n "$TP_TARGET_ADDRS" ] && [ "$TP_TARGET_ADDRS" != "[]" ]; then
    echo "$TP_TARGET_ADDRS" | jq -r '.[]' > /tmp/targets.txt
    while IFS= read -r tgt; do
        set -- "$@" "-target=$tgt"
    done < /tmp/targets.txt
    rm -f /tmp/targets.txt
    echo "[entrypoint] Using targets: $TP_TARGET_ADDRS"
fi

# --- Build -replace arguments from TP_REPLACE_ADDRS JSON (plan phase only) ---
if [ "$TP_PHASE" = "plan" ] && [ -n "$TP_REPLACE_ADDRS" ] && [ "$TP_REPLACE_ADDRS" != "[]" ]; then
    echo "$TP_REPLACE_ADDRS" | jq -r '.[]' > /tmp/replaces.txt
    while IFS= read -r rpl; do
        set -- "$@" "-replace=$rpl"
    done < /tmp/replaces.txt
    rm -f /tmp/replaces.txt
    echo "[entrypoint] Using replace addrs: $TP_REPLACE_ADDRS"
fi

# --- Execute phase ---
EXIT_CODE=0

if [ "$TP_PHASE" = "plan" ]; then
    echo "[entrypoint] Running $TP_BACKEND plan..."
    # Redirect to file (not tee) so $! gives the plan PID for correct exit
    # code capture and signal forwarding. Output shown via cat afterwards.
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
    "$TP_BIN" plan $PLAN_ARGS "$@" > /tmp/plan.log 2>&1 &
    CHILD_PID=$!
    wait "$CHILD_PID" || EXIT_CODE=$?
    CHILD_PID=""

    # Show plan output in pod logs
    cat /tmp/plan.log 2>/dev/null || true

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
        curl -sSf -X POST -H "$AUTH_HEADER" \
            -H "Content-Type: application/json" \
            -d "{\"has_changes\": $HAS_CHANGES_JSON}" \
            "${TP_API_URL}/api/v2/runs/${TP_RUN_ID}/plan-result" || true
    fi

    # Upload plan log
    if [ -n "$TP_API_URL" ] && [ -f /tmp/plan.log ]; then
        curl -sSf -X PUT -H "$AUTH_HEADER" \
            -H "Content-Type: application/octet-stream" \
            --data-binary @/tmp/plan.log \
            "${TP_API_URL}/api/v2/runs/${TP_RUN_ID}/artifacts/plan-log" || true
    fi

    # Upload plan file
    if [ -n "$TP_API_URL" ] && [ -f tfplan ]; then
        curl -sSf -X PUT -H "$AUTH_HEADER" \
            -H "Content-Type: application/octet-stream" \
            --data-binary @tfplan \
            "${TP_API_URL}/api/v2/runs/${TP_RUN_ID}/artifacts/plan-file" || true
    fi

elif [ "$TP_PHASE" = "apply" ]; then
    # Download plan file from plan phase
    if [ -n "$TP_API_URL" ] && [ -n "$TP_RUN_ID" ]; then
        echo "[entrypoint] Downloading plan file from plan phase..."
        tp_curl_download tfplan -H "$AUTH_HEADER" \
            "${TP_API_URL}/api/v2/runs/${TP_RUN_ID}/artifacts/plan-file" 2>/dev/null || true
    fi

    echo "[entrypoint] Running $TP_BACKEND apply..."
    if [ -f tfplan ]; then
        # Plan file already includes var-file inputs — no need to re-specify
        "$TP_BIN" apply -input=false tfplan > /tmp/apply.log 2>&1 &
    else
        "$TP_BIN" apply -input=false -auto-approve "$@" > /tmp/apply.log 2>&1 &
    fi
    CHILD_PID=$!
    wait "$CHILD_PID" || EXIT_CODE=$?
    CHILD_PID=""

    # Show apply output in pod logs
    cat /tmp/apply.log 2>/dev/null || true

    # Upload apply log
    if [ -n "$TP_API_URL" ] && [ -f /tmp/apply.log ]; then
        curl -sSf -X PUT -H "$AUTH_HEADER" \
            -H "Content-Type: application/octet-stream" \
            --data-binary @/tmp/apply.log \
            "${TP_API_URL}/api/v2/runs/${TP_RUN_ID}/artifacts/apply-log" || true
    fi

    # Upload new state
    if [ -n "$TP_API_URL" ] && [ -f terraform.tfstate ]; then
        curl -sSf -X PUT -H "$AUTH_HEADER" \
            -H "Content-Type: application/octet-stream" \
            --data-binary @terraform.tfstate \
            "${TP_API_URL}/api/v2/runs/${TP_RUN_ID}/artifacts/state" || true
    fi
fi

echo "[entrypoint] Phase $TP_PHASE completed with exit code $EXIT_CODE"
exit $EXIT_CODE
