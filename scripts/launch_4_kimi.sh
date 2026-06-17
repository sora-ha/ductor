#!/usr/bin/env bash
# Launch 4 isolated ductor instances, each using the Kimi provider and a
# separate DUCTOR_HOME.  Each instance gets its own Telegram bot token, PID
# lock, inter-agent port, and workspace.
#
# Usage:
#   ./scripts/launch_4_kimi.sh <token1> <token2> <token3> <token4> [allowed_user_id]
#
# Example:
#   ./scripts/launch_4_kimi.sh \
#     "123:abc" "456:def" "789:ghi" "012:jkl" 12345678

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ $# -lt 4 ]]; then
    echo "Usage: $0 <token1> <token2> <token3> <token4> [allowed_user_id]" >&2
    exit 1
fi

TOKENS=("$1" "$2" "$3" "$4")
ALLOWED_USER_ID="${5:-123456789}"

# Each instance needs a free inter-agent port.  Adjust if these are in use.
INTERAGENT_PORTS=(8801 8802 8803 8804)

# Base directory for the per-instance homes.  Override with DUCTOR_BASE_HOME.
BASE_HOME="${DUCTOR_BASE_HOME:-${HOME}/.ductor-kimi}"

PYTHON="${PYTHON:-python3}"

# Prefer the installed `ductor` command; fall back to running from the repo.
if command -v ductor >/dev/null 2>&1; then
    DUCTOR_CMD=(ductor)
elif [[ -f "${REPO_ROOT}/ductor_bot/__main__.py" ]]; then
    DUCTOR_CMD=("${PYTHON}" -m ductor_bot)
else
    echo "Cannot find 'ductor' command or repo entrypoint." >&2
    exit 1
fi

# Source config template (repo or installed fallback).
TEMPLATE="${REPO_ROOT}/config.example.json"
if [[ ! -f "${TEMPLATE}" ]]; then
    TEMPLATE=$("${PYTHON}" - <<'PY'
import importlib.util
import pathlib
spec = importlib.util.find_spec("ductor_bot")
if spec and spec.origin:
    print(pathlib.Path(spec.origin).parent / "_config_example.json")
PY
    )
fi

if [[ ! -f "${TEMPLATE}" ]]; then
    echo "Cannot find config.example.json or packaged _config_example.json" >&2
    exit 1
fi

mkdir -p "${BASE_HOME}"

PIDS=()

for i in {0..3}; do
    INSTANCE_NUM=$((i + 1))
    HOME_DIR="${BASE_HOME}-${INSTANCE_NUM}"
    CONFIG_DIR="${HOME_DIR}/config"
    CONFIG_FILE="${CONFIG_DIR}/config.json"
    LOG_DIR="${HOME_DIR}/logs"

    mkdir -p "${CONFIG_DIR}" "${LOG_DIR}"

    # Generate a per-instance config from the template.
    "${PYTHON}" - <<PY
import json
from pathlib import Path

template = Path("${TEMPLATE}")
config = json.loads(template.read_text(encoding="utf-8"))

config["provider"] = "kimi"
config["model"] = "kimi-code/kimi-for-coding"
config["telegram_token"] = "${TOKENS[$i]}"
config["allowed_user_ids"] = [${ALLOWED_USER_ID}]
config["transports"] = ["telegram"]
config["transport"] = "telegram"
config["interagent_port"] = ${INTERAGENT_PORTS[$i]}
config["docker"]["enabled"] = False
config["webhooks"]["enabled"] = False
config["api"]["enabled"] = False

Path("${CONFIG_FILE}").write_text(json.dumps(config, indent=4), encoding="utf-8")
PY

    echo "[instance ${INSTANCE_NUM}] DUCTOR_HOME=${HOME_DIR} port=${INTERAGENT_PORTS[$i]}"

    # Launch in the background with its own DUCTOR_HOME.
    DUCTOR_HOME="${HOME_DIR}" "${DUCTOR_CMD[@]}" >"${LOG_DIR}/launcher.log" 2>&1 &
    PIDS+=("$!")
    echo "[instance ${INSTANCE_NUM}] PID=$!"
done

echo "All 4 Kimi ductor instances launched.  Press Ctrl-C to stop them."

# Forward Ctrl-C to all children.
cleanup() {
    echo "Stopping instances..."
    for pid in "${PIDS[@]}"; do
        kill "${pid}" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    echo "Stopped."
}
trap cleanup INT TERM

# Wait until all background processes exit.
wait
