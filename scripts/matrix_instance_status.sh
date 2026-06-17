#!/usr/bin/env bash
# Show Matrix join/invite state for each kimi-matrix ductor instance.
set -euo pipefail

HOMESERVER="${1:-http://matrix.local:6167}"
BASE_HOME="${DUCTOR_BASE_HOME:-${HOME}/.ductor-kimi-matrix}"

for i in 1 2 3 4; do
    HOME_DIR="${BASE_HOME}-${i}"
    CREDS="${HOME_DIR}/matrix_store/credentials.json"
    echo "========== instance ${i} =========="
    if [[ ! -f "${CREDS}" ]]; then
        echo "  (no credentials)"
        echo
        continue
    fi
    python3 - <<PY
import json, urllib.request, urllib.parse
from pathlib import Path

creds = json.loads(Path("${CREDS}").read_text())
user = creds["user_id"]
token = creds["access_token"]
req = urllib.request.Request(
    "${HOMESERVER}/_matrix/client/v3/sync?timeout=0",
    headers={"Authorization": f"Bearer {token}"},
)
data = json.loads(urllib.request.urlopen(req, timeout=10).read())
rooms = data.get("rooms", {})
joined = list(rooms.get("join", {}).keys())
invited = list(rooms.get("invite", {}).keys())
print(f"  user:    {user}")
print(f"  joined:  {len(joined)} {joined}")
print(f"  invited: {len(invited)} {invited}")
if not joined and not invited:
    print("  -> not in any room; send a NEW DM from Element to this bot")
PY
    if [[ -f "${HOME_DIR}/logs/agent.log" ]]; then
        echo "  recent:"
        grep -E 'invite received|Auto-joined|Failed to join|Message received' \
            "${HOME_DIR}/logs/agent.log" | tail -3 | sed 's/^/    /' || true
    fi
    echo
done
