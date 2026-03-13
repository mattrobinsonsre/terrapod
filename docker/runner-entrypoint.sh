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
    curl -sSfL -H "$AUTH_HEADER" "$BINARY_URL" -o "/tmp/${TP_BACKEND}.zip"
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
    HTTP_CODE=$(curl -sSf -o /tmp/config.tar.gz -w "%{http_code}" -L -H "$AUTH_HEADER" \
        "${TP_API_URL}/api/v2/runs/${TP_RUN_ID}/artifacts/config" 2>/dev/null) || true
    if [ "$HTTP_CODE" = "200" ] && [ -f /tmp/config.tar.gz ]; then
        tar xzf /tmp/config.tar.gz -C "$WORK_DIR"

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
    curl -sSfL -H "$AUTH_HEADER" \
        "${TP_API_URL}/api/v2/runs/${TP_RUN_ID}/artifacts/state" \
        -o "$WORK_DIR/terraform.tfstate" 2>/dev/null || true
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
    "$TP_BIN" plan $PLAN_ARGS "$@" > /tmp/plan.log 2>&1 &
    CHILD_PID=$!
    wait "$CHILD_PID" || EXIT_CODE=$?
    CHILD_PID=""

    # Show plan output in pod logs
    cat /tmp/plan.log 2>/dev/null || true

    # -detailed-exitcode: 0=no changes, 1=error, 2=changes present
    if [ "$EXIT_CODE" = "2" ]; then
        echo "[entrypoint] PLAN_HAS_CHANGES=true"
        EXIT_CODE=0
    elif [ "$EXIT_CODE" = "0" ]; then
        echo "[entrypoint] PLAN_HAS_CHANGES=false"
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
        curl -sSfL -H "$AUTH_HEADER" \
            "${TP_API_URL}/api/v2/runs/${TP_RUN_ID}/artifacts/plan-file" \
            -o tfplan 2>/dev/null || true
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
