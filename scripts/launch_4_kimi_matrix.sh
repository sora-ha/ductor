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
#   When created via register_matrix_bots.py, a companion file
#   matrix_instances_rooms.json is written and used to pre-configure
#   matrix.allowed_rooms for each instance.
#
# Example:
#   ./scripts/launch_4_kimi_matrix.sh https://matrix.example.com @you:example.com
#
# Optional environment variables:
#   DUCTOR_LANGUAGE          ductor UI language (en, de, nl, fr, ru, es, pt, id). Default: en
#   AGENT_RESPONSE_LANGUAGE  preferred Kimi reply language (free text, e.g. Cantonese / 粵語)
#   DUCTOR_BASE_HOME         base path for per-instance homes (default: ~/.ductor-kimi-matrix)
#   DUCTOR_SCREEN_PREFIX     GNU screen session name prefix (default: ductor-kimi-matrix)
#
# Each instance runs in a detached screen session. Attach with:
#   screen -r ductor-kimi-matrix-1
# List sessions:
#   screen -ls | grep ductor-kimi-matrix
# Stop all:
#   for i in 1 2 3 4; do screen -S ductor-kimi-matrix-$i -X quit; done

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
SCREEN_PREFIX="${DUCTOR_SCREEN_PREFIX:-ductor-kimi-matrix}"
DUCTOR_LANGUAGE="${DUCTOR_LANGUAGE:-en}"
AGENT_RESPONSE_LANGUAGE="${AGENT_RESPONSE_LANGUAGE:-}"
PYTHON="${PYTHON:-python3}"

if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    PYTHON="${REPO_ROOT}/.venv/bin/python"
fi
export PYTHON

if ! command -v screen >/dev/null 2>&1; then
    echo "GNU screen is required. Install it (e.g. brew install screen) and retry." >&2
    exit 1
fi

quote_cmd() {
    local quoted=()
    local part
    for part in "$@"; do
        quoted+=("$(printf '%q' "${part}")")
    done
    (IFS=' '; echo "${quoted[*]}")
}

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

# Read non-empty lines from the instances file (bash 3.x has no mapfile).
LINES=()
while IFS= read -r line; do
    LINES+=("${line}")
done < <(grep -v '^\s*$' "${INSTANCES_FILE}")
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

DUCTOR_EXEC="$(quote_cmd "${DUCTOR_CMD[@]}")"
SESSIONS=()

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

    # Generate per-instance config (skip when matrix-crew wizard already wrote one).
    if [[ -f "${CONFIG_FILE}" && "${DUCTOR_FORCE_CONFIG_GEN:-}" != "1" ]]; then
        echo "[instance ${INSTANCE_NUM}] reusing existing config at ${CONFIG_FILE}"
    else
    "${PYTHON}" - <<PY
import json
from pathlib import Path

template = Path("${TEMPLATE}")
config = json.loads(template.read_text(encoding="utf-8"))

config["interagent_port"] = ${INTERAGENT_PORTS[$i]}
config["ductor_home"] = "${HOME_DIR}"
config["language"] = "${DUCTOR_LANGUAGE}"

matrix = config.setdefault("matrix", {})
matrix["homeserver"] = "${HOMESERVER}"
matrix["user_id"] = "${USER_ID}"
matrix["password"] = "${PASSWORD}"
matrix["access_token"] = ""
matrix["device_id"] = ""
matrix["allowed_users"] = ["${ALLOWED_USER}"]

rooms_path = Path("${INSTANCES_FILE}").resolve().parent / (
    Path("${INSTANCES_FILE}").resolve().stem + "_rooms.json"
)
if rooms_path.is_file():
    rooms_data = json.loads(rooms_path.read_text(encoding="utf-8"))
    room_id = rooms_data.get("rooms", {}).get("${USER_ID}", "")
    if room_id:
        matrix["allowed_rooms"] = [room_id]

Path("${CONFIG_FILE}").write_text(json.dumps(config, indent=4), encoding="utf-8")
PY
    fi

    if [[ -n "${AGENT_RESPONSE_LANGUAGE}" ]]; then
        MEMORY_DIR="${HOME_DIR}/workspace/memory_system"
        MEMORY_FILE="${MEMORY_DIR}/MAINMEMORY.md"
        mkdir -p "${MEMORY_DIR}"
        "${PYTHON}" - <<PY
from pathlib import Path

memory_file = Path("${MEMORY_FILE}")
preference = "- User prefers responses in ${AGENT_RESPONSE_LANGUAGE}."
marker = "## Decisions and Preferences"
body = memory_file.read_text(encoding="utf-8") if memory_file.exists() else ""

if preference not in body:
    if body.strip():
        if marker in body:
            body = body.replace(
                f"{marker}\\n\\n(Empty -- record important decisions and their reasoning here.)",
                f"{marker}\\n\\n{preference}",
            )
            if preference not in body:
                body = body.rstrip() + f"\\n\\n{marker}\\n\\n{preference}\\n"
        else:
            body = body.rstrip() + f"\\n\\n{marker}\\n\\n{preference}\\n"
    else:
        body = f"""# Main Memory

## About the User

(Empty -- will be populated as you learn about your human.)

## Learned Facts

(Empty -- will be populated as the agent learns.)

## Decisions and Preferences

{preference}
"""
    memory_file.write_text(body, encoding="utf-8")
PY
    fi

    SESSION_NAME="${SCREEN_PREFIX}-${INSTANCE_NUM}"
    SESSIONS+=("${SESSION_NAME}")

    echo "[instance ${INSTANCE_NUM}] DUCTOR_HOME=${HOME_DIR} user=${USER_ID} port=${INTERAGENT_PORTS[$i]} language=${DUCTOR_LANGUAGE}"

    # Replace any existing session with the same name.
    screen -S "${SESSION_NAME}" -X quit >/dev/null 2>&1 || true

    screen -dmS "${SESSION_NAME}" bash -c "
        export PATH=$(printf '%q' "${REPO_ROOT}/.venv/bin"):\$PATH
        export DUCTOR_HOME=$(printf '%q' "${HOME_DIR}")
        cd $(printf '%q' "${REPO_ROOT}")
        exec ${DUCTOR_EXEC} >> $(printf '%q' "${LOG_DIR}/launcher.log") 2>&1
    "

    echo "[instance ${INSTANCE_NUM}] screen session=${SESSION_NAME}"
done

echo
echo "All 4 Matrix/Kimi ductor instances launched in detached screen sessions."
echo
echo "Attach to a session:"
for session in "${SESSIONS[@]}"; do
    echo "  screen -r ${session}"
done
echo
echo "List sessions:  screen -ls | grep ${SCREEN_PREFIX}"
echo "Stop all:"
echo "  for i in 1 2 3 4; do screen -S ${SCREEN_PREFIX}-\$i -X quit; done"
