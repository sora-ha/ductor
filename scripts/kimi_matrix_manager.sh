#!/usr/bin/env bash
# Manage 4 Kimi/Matrix ductor instances launched via GNU screen.
#
# Usage:
#   ./scripts/kimi_matrix_manager.sh <command> [args...]
#
# Commands:
#   start   [homeserver] [allowed_user] [instances_file]
#   stop
#   restart [homeserver] [allowed_user] [instances_file]
#   status  [--matrix]          # screen sessions (+ optional Matrix room state)
#   logs    [1-4] [-f]          # tail launcher.log (default: all instances)
#   attach  <1-4>                 # attach to a screen session
#
# Environment (optional):
#   MATRIX_HOMESERVER         default: from instance 1 config, else http://matrix.test:6167
#   MATRIX_ALLOWED_USER       default: from instance 1 config, else @wingkit:matrix.test
#   MATRIX_INSTANCES_FILE       default: scripts/matrix_instances.txt
#   DUCTOR_BASE_HOME          default: ~/.ductor-kimi-matrix
#   DUCTOR_SCREEN_PREFIX      default: ductor-kimi-matrix
#   INSTANCE_COUNT            default: 4
#   PYTHON                    default: repo .venv python or python3

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

INSTANCE_COUNT="${INSTANCE_COUNT:-4}"
BASE_HOME="${DUCTOR_BASE_HOME:-${HOME}/.ductor-kimi-matrix}"
SCREEN_PREFIX="${DUCTOR_SCREEN_PREFIX:-ductor-kimi-matrix}"
INSTANCES_FILE="${MATRIX_INSTANCES_FILE:-${SCRIPT_DIR}/matrix_instances.txt}"
HOMESERVER="${MATRIX_HOMESERVER:-}"
ALLOWED_USER="${MATRIX_ALLOWED_USER:-}"
PYTHON="${PYTHON:-python3}"

if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    PYTHON="${REPO_ROOT}/.venv/bin/python"
fi
export PYTHON
export PATH="${REPO_ROOT}/.venv/bin:${PATH}"

session_name() {
    echo "${SCREEN_PREFIX}-$1"
}

match_screen() {
    local session="$1"
    local list
    list="$({ screen -ls 2>/dev/null || true; })"
    grep -F ".${session}" <<< "${list}" | grep -qv "(Dead)"
}

apply_config_defaults() {
    local config="${BASE_HOME}-1/config/config.json"
    if [[ ! -f "${config}" ]]; then
        return 0
    fi
    if [[ -n "${HOMESERVER}" && -n "${ALLOWED_USER}" ]]; then
        return 0
    fi
    eval "$("${PYTHON}" - <<PY
import json
from pathlib import Path

config = json.loads(Path("${config}").read_text(encoding="utf-8"))
matrix = config.get("matrix", {})
homeserver = matrix.get("homeserver", "")
allowed = (matrix.get("allowed_users") or [""])[0]
if not "${HOMESERVER}" and homeserver:
    print(f"export HOMESERVER={homeserver!r}")
if not "${ALLOWED_USER}" and allowed:
    print(f"export ALLOWED_USER={allowed!r}")
PY
)"
}

usage() {
    cat <<EOF
Usage: $(basename "$0") <command> [args...]

Commands:
  start   [homeserver] [allowed_user] [instances_file]
  stop
  restart [homeserver] [allowed_user] [instances_file]
  status  [--matrix]
  logs    [1-4] [-f]
  attach  <1-4>

Environment:
  MATRIX_HOMESERVER, MATRIX_ALLOWED_USER, MATRIX_INSTANCES_FILE
  DUCTOR_BASE_HOME, DUCTOR_SCREEN_PREFIX, INSTANCE_COUNT, PYTHON
EOF
}

cmd_start() {
    apply_config_defaults
    HOMESERVER="${1:-${HOMESERVER:-http://matrix.test:6167}}"
    ALLOWED_USER="${2:-${ALLOWED_USER:-@wingkit:matrix.test}}"
    INSTANCES_FILE="${3:-${INSTANCES_FILE}}"

    exec "${SCRIPT_DIR}/launch_4_kimi_matrix.sh" \
        "${HOMESERVER}" "${ALLOWED_USER}" "${INSTANCES_FILE}"
}

cmd_stop() {
    local i session
    for ((i = 1; i <= INSTANCE_COUNT; i++)); do
        session="$(session_name "${i}")"
        if match_screen "${session}"; then
            echo "[instance ${i}] stopping ${session}"
            screen -S "${session}" -X quit >/dev/null 2>&1 || true
        else
            echo "[instance ${i}] not running (${session})"
        fi
    done
}

cmd_restart() {
    cmd_stop
    sleep 1
    cmd_start "$@"
}

cmd_status() {
    local show_matrix=0
    if [[ "${1:-}" == "--matrix" ]]; then
        show_matrix=1
    fi

    local i session running=0
    for ((i = 1; i <= INSTANCE_COUNT; i++)); do
        session="$(session_name "${i}")"
        echo "========== instance ${i} =========="
        if match_screen "${session}"; then
            running=$((running + 1))
            echo "  screen:  running (${session})"
        else
            echo "  screen:  stopped"
        fi
        local home="${BASE_HOME}-${i}"
        local config="${home}/config/config.json"
        if [[ -f "${config}" ]]; then
            "${PYTHON}" - <<PY
import json
from pathlib import Path

config = json.loads(Path("${config}").read_text(encoding="utf-8"))
matrix = config.get("matrix", {})
print(f"  user:    {matrix.get('user_id', '(unknown)')}")
print(f"  port:    {config.get('interagent_port', '(unknown)')}")
print(f"  home:    ${home}")
print(f"  log:     ${home}/logs/launcher.log")
PY
        else
            echo "  (no config)"
        fi
        echo
    done

    echo "Summary: ${running}/${INSTANCE_COUNT} screen sessions running"

    if [[ "${show_matrix}" -eq 1 ]]; then
        echo
        exec "${SCRIPT_DIR}/matrix_instance_status.sh" "${HOMESERVER:-http://matrix.test:6167}"
    fi
}

cmd_logs() {
    local follow=0
    local target=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -f|--follow)
                follow=1
                shift
                ;;
            [1-9]|1[0-9])
                target="$1"
                shift
                ;;
            *)
                echo "Unknown logs argument: $1" >&2
                exit 1
                ;;
        esac
    done

    if [[ -n "${target}" ]]; then
        local log="${BASE_HOME}-${target}/logs/launcher.log"
        if [[ ! -f "${log}" ]]; then
            echo "Log not found: ${log}" >&2
            exit 1
        fi
        if [[ "${follow}" -eq 1 ]]; then
            tail -f "${log}"
        else
            tail -20 "${log}"
        fi
        return 0
    fi

    local i log
    for ((i = 1; i <= INSTANCE_COUNT; i++)); do
        log="${BASE_HOME}-${i}/logs/launcher.log"
        echo "========== instance ${i} =========="
        if [[ ! -f "${log}" ]]; then
            echo "  (no log)"
            echo
            continue
        fi
        if [[ "${follow}" -eq 1 ]]; then
            echo "Cannot follow multiple logs; use: $(basename "$0") logs ${i} -f" >&2
            exit 1
        fi
        tail -5 "${log}" | sed 's/^/  /'
        echo
    done
}

cmd_attach() {
    local num="${1:-}"
    if [[ -z "${num}" || ! "${num}" =~ ^[1-9][0-9]*$ ]]; then
        echo "Usage: $(basename "$0") attach <1-${INSTANCE_COUNT}>" >&2
        exit 1
    fi
    if [[ "${num}" -gt "${INSTANCE_COUNT}" ]]; then
        echo "Instance must be 1-${INSTANCE_COUNT}" >&2
        exit 1
    fi

    local session
    session="$(session_name "${num}")"
    if ! match_screen "${session}"; then
        echo "Session not running: ${session}" >&2
        exit 1
    fi
    exec screen -r "${session}"
}

main() {
    if [[ $# -lt 1 ]]; then
        usage >&2
        exit 1
    fi

    local cmd="$1"
    shift

    case "${cmd}" in
        start)
            cmd_start "$@"
            ;;
        stop)
            cmd_stop
            ;;
        restart)
            cmd_restart "$@"
            ;;
        status)
            cmd_status "$@"
            ;;
        logs)
            cmd_logs "$@"
            ;;
        attach)
            cmd_attach "$@"
            ;;
        -h|--help|help)
            usage
            ;;
        *)
            echo "Unknown command: ${cmd}" >&2
            usage >&2
            exit 1
            ;;
    esac
}

main "$@"
