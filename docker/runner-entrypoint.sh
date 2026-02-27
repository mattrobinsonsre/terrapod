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

# --- Initialize ---
echo "[entrypoint] Running $TP_BACKEND init..."
"$TP_BIN" init -input=false -no-color 2>&1

# --- Execute phase ---
EXIT_CODE=0

if [ "$TP_PHASE" = "plan" ]; then
    echo "[entrypoint] Running $TP_BACKEND plan..."
    "$TP_BIN" plan -input=false -no-color -out=tfplan 2>&1 | tee /tmp/plan.log &
    CHILD_PID=$!
    wait "$CHILD_PID" || EXIT_CODE=$?
    CHILD_PID=""

    # Upload plan log
    if [ -n "$TP_PLAN_LOG_UPLOAD_URL" ] && [ -f /tmp/plan.log ]; then
        curl -sSf -X PUT --data-binary @/tmp/plan.log "$TP_PLAN_LOG_UPLOAD_URL" || true
    fi

    # Upload plan file
    if [ -n "$TP_PLAN_FILE_UPLOAD_URL" ] && [ -f tfplan ]; then
        curl -sSf -X PUT --data-binary @tfplan "$TP_PLAN_FILE_UPLOAD_URL" || true
    fi

elif [ "$TP_PHASE" = "apply" ]; then
    # Download plan file from plan phase (if available)
    if [ -n "$TP_PLAN_FILE_DOWNLOAD_URL" ]; then
        echo "[entrypoint] Downloading plan file from plan phase..."
        curl -sSfL "$TP_PLAN_FILE_DOWNLOAD_URL" -o tfplan
    fi

    echo "[entrypoint] Running $TP_BACKEND apply..."
    if [ -f tfplan ]; then
        "$TP_BIN" apply -input=false -no-color tfplan 2>&1 | tee /tmp/apply.log &
    else
        "$TP_BIN" apply -input=false -no-color -auto-approve 2>&1 | tee /tmp/apply.log &
    fi
    CHILD_PID=$!
    wait "$CHILD_PID" || EXIT_CODE=$?
    CHILD_PID=""

    # Upload apply log
    if [ -n "$TP_APPLY_LOG_UPLOAD_URL" ] && [ -f /tmp/apply.log ]; then
        curl -sSf -X PUT --data-binary @/tmp/apply.log "$TP_APPLY_LOG_UPLOAD_URL" || true
    fi

    # Upload new state
    if [ -n "$TP_STATE_UPLOAD_URL" ] && [ -f terraform.tfstate ]; then
        curl -sSf -X PUT --data-binary @terraform.tfstate "$TP_STATE_UPLOAD_URL" || true
    fi
fi

echo "[entrypoint] Phase $TP_PHASE completed with exit code $EXIT_CODE"
exit $EXIT_CODE
