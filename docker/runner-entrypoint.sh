#!/bin/sh
set -e

# Signal-forwarding entrypoint for Terrapod runner Jobs.
#
# Traps SIGTERM/SIGQUIT and forwards them to the terraform/tofu child process
# so it can release state locks and exit cleanly. This is critical for spot
# instance preemption — K8s sends SIGTERM, and we have 120s
# (terminationGracePeriodSeconds) before SIGKILL.

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

# --- Download binary from cache ---
if [ -n "$TP_BINARY_URL" ]; then
    echo "[entrypoint] Downloading $TP_BACKEND $TP_VERSION from binary cache..."
    curl -sSfL "$TP_BINARY_URL" -o "/tmp/${TP_BACKEND}.zip"
    unzip -o -q "/tmp/${TP_BACKEND}.zip" -d /tmp/bin
    chmod +x "/tmp/bin/${TP_BACKEND}"
    TP_BIN="/tmp/bin/${TP_BACKEND}"
else
    echo "[entrypoint] No binary cache URL, expecting $TP_BACKEND on PATH"
    TP_BIN="$TP_BACKEND"
fi

# --- Download configuration archive ---
if [ -n "$TP_CONFIG_URL" ]; then
    echo "[entrypoint] Downloading configuration..."
    curl -sSfL "$TP_CONFIG_URL" -o /tmp/config.tar.gz
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
fi

# --- Download current state ---
if [ -n "$TP_STATE_URL" ]; then
    echo "[entrypoint] Downloading current state..."
    curl -sSfL "$TP_STATE_URL" -o "$WORK_DIR/terraform.tfstate" || true
fi

# --- Run setup script (if configured) ---
if [ -n "$TP_SETUP_SCRIPT" ]; then
    echo "[entrypoint] Running setup script..."
    eval "$TP_SETUP_SCRIPT"
fi

# --- Configure provider mirror ---
# Only configure network mirror for HTTPS URLs (terraform/tofu require HTTPS)
if [ -n "$TP_API_URL" ]; then
    case "$TP_API_URL" in
        https://*)
            cat > /tmp/terraform.rc <<TFEOF
provider_installation {
  network_mirror {
    url = "${TP_API_URL}/v1/providers/"
  }
}
TFEOF
            export TF_CLI_CONFIG_FILE="/tmp/terraform.rc"
            echo "[entrypoint] Provider mirror configured: ${TP_API_URL}/v1/providers/"
            ;;
        *)
            echo "[entrypoint] Skipping provider mirror (requires HTTPS): ${TP_API_URL}"
            ;;
    esac
fi

# --- Initialize ---
echo "[entrypoint] Running $TP_BACKEND init..."
"$TP_BIN" init -input=false 2>&1

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
    if [ -n "$TP_PLAN_LOG_UPLOAD_URL" ] && [ -f /tmp/plan.log ]; then
        curl -sSf -X PUT -H "Content-Type: application/octet-stream" --data-binary @/tmp/plan.log "$TP_PLAN_LOG_UPLOAD_URL" || true
    fi

    # Upload plan file
    if [ -n "$TP_PLAN_FILE_UPLOAD_URL" ] && [ -f tfplan ]; then
        curl -sSf -X PUT -H "Content-Type: application/octet-stream" --data-binary @tfplan "$TP_PLAN_FILE_UPLOAD_URL" || true
    fi

elif [ "$TP_PHASE" = "apply" ]; then
    # Download plan file from plan phase (if available)
    if [ -n "$TP_PLAN_FILE_DOWNLOAD_URL" ]; then
        echo "[entrypoint] Downloading plan file from plan phase..."
        curl -sSfL "$TP_PLAN_FILE_DOWNLOAD_URL" -o tfplan
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
    if [ -n "$TP_APPLY_LOG_UPLOAD_URL" ] && [ -f /tmp/apply.log ]; then
        curl -sSf -X PUT -H "Content-Type: application/octet-stream" --data-binary @/tmp/apply.log "$TP_APPLY_LOG_UPLOAD_URL" || true
    fi

    # Upload new state
    if [ -n "$TP_STATE_UPLOAD_URL" ] && [ -f terraform.tfstate ]; then
        curl -sSf -X PUT -H "Content-Type: application/octet-stream" --data-binary @terraform.tfstate "$TP_STATE_UPLOAD_URL" || true
    fi
fi

echo "[entrypoint] Phase $TP_PHASE completed with exit code $EXIT_CODE"
exit $EXIT_CODE
