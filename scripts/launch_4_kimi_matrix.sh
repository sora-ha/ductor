#!/usr/bin/env bash
# Launch 4 isolated ductor instances using the Kimi provider and Matrix transport.
# Each instance gets its own DUCTOR_HOME, Matrix account, PID lock, inter-agent
# port, and workspace.
#
# Usage:
#   ./scripts/launch_4_kimi_matrix.sh <homeserver> <allowed_user> [instances_file]
#
#   instances_file format (one instance per line):
#     @bot1:example.com password1
#     @bot2:example.com password2
#     @bot3:example.com password3
#     @bot4:example.com password4
#
#   If no file is given, defaults to ./matrix_instances.txt.
#
# Example:
#   ./scripts/launch_4_kimi_matrix.sh https://matrix.example.com @you:example.com

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <homeserver> <allowed_user> [instances_file]" >&2
    exit 1
fi

HOMESERVER="$1"
ALLOWED_USER="$2"
INSTANCES_FILE="${3:-${SCRIPT_DIR}/matrix_instances.txt}"

INTERAGENT_PORTS=(8801 8802 8803 8804)
BASE_HOME="${DUCTOR_BASE_HOME:-${HOME}/.ductor-kimi-matrix}"
PYTHON="${PYTHON:-python3}"

if command -v ductor >/dev/null 2>&1; then
    DUCTOR_CMD=(ductor)
elif [[ -f "${REPO_ROOT}/ductor_bot/__main__.py" ]]; then
    DUCTOR_CMD=("${PYTHON}" -m ductor_bot)
else
    echo "Cannot find 'ductor' command or repo entrypoint." >&2
    exit 1
fi

TEMPLATE="${REPO_ROOT}/config.example.multi-kimi.json"
if [[ ! -f "${TEMPLATE}" ]]; then
    echo "Cannot find config.example.multi-kimi.json" >&2
    exit 1
fi

if [[ ! -f "${INSTANCES_FILE}" ]]; then
    echo "Instances file not found: ${INSTANCES_FILE}" >&2
    cat <<EOF >&2
Create it with 4 lines, one per Matrix bot:

@bot1:example.com password1
@bot2:example.com password2
@bot3:example.com password3
@bot4:example.com password4
EOF
    exit 1
fi

# Read non-empty lines from the instances file.
mapfile -t LINES < <(grep -v '^\s*$' "${INSTANCES_FILE}")
if [[ ${#LINES[@]} -lt 4 ]]; then
    echo "Instances file must contain at least 4 non-empty lines (got ${#LINES[@]})." >&2
    exit 1
fi

# Try a quick import of matrix-nio so the failures happen up front.
if ! "${PYTHON}" -c 'import nio' >/dev/null 2>&1; then
    echo "Matrix support not installed. Run: pip install 'ductor[matrix]'" >&2
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

    # Parse user_id and password from the instances file.
    LINE="${LINES[$i]}"
    USER_ID="$(echo "${LINE}" | awk '{print $1}')"
    PASSWORD="$(echo "${LINE}" | cut -d' ' -f2-)"

    if [[ -z "${USER_ID}" || -z "${PASSWORD}" ]]; then
        echo "Invalid line ${INSTANCE_NUM} in ${INSTANCES_FILE}: '${LINE}'" >&2
        exit 1
    fi

    # Generate per-instance config.
    "${PYTHON}" - <<PY
import json
from pathlib import Path

template = Path("${TEMPLATE}")
config = json.loads(template.read_text(encoding="utf-8"))

config["interagent_port"] = ${INTERAGENT_PORTS[$i]}

matrix = config.setdefault("matrix", {})
matrix["homeserver"] = "${HOMESERVER}"
matrix["user_id"] = "${USER_ID}"
matrix["password"] = "${PASSWORD}"
matrix["access_token"] = ""
matrix["device_id"] = ""
matrix["allowed_users"] = ["${ALLOWED_USER}"]

Path("${CONFIG_FILE}").write_text(json.dumps(config, indent=4), encoding="utf-8")
PY

    echo "[instance ${INSTANCE_NUM}] DUCTOR_HOME=${HOME_DIR} user=${USER_ID} port=${INTERAGENT_PORTS[$i]}"

    DUCTOR_HOME="${HOME_DIR}" "${DUCTOR_CMD[@]}" >"${LOG_DIR}/launcher.log" 2>&1 &
    PIDS+=("$!")
    echo "[instance ${INSTANCE_NUM}] PID=$!"
done

echo "All 4 Matrix/Kimi ductor instances launched.  Press Ctrl-C to stop them."

cleanup() {
    echo "Stopping instances..."
    for pid in "${PIDS[@]}"; do
        kill "${pid}" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    echo "Stopped."
}
trap cleanup INT TERM

wait
