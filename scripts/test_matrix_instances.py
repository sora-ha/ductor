#!/usr/bin/env python3
"""Validate matrix_instances.txt and matrix_instances_rooms.json.

Checks file format, cross-file consistency, bot logins, and room membership
on the configured homeserver.

Usage:
    cd ductor
    uv run python scripts/test_matrix_instances.py

    uv run python scripts/test_matrix_instances.py \\
        --instances scripts/matrix_instances.txt \\
        --owner-password changeme
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import aiohttp


class MatrixApiError(RuntimeError):
    def __init__(self, status: int, data: dict[str, Any]) -> None:
        self.status = status
        self.data = data
        err = data.get("errcode", status)
        msg = data.get("error", data)
        super().__init__(f"{err}: {msg}")


def _parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Test matrix_instances.txt and matrix_instances_rooms.json."
    )
    parser.add_argument(
        "--instances",
        default=str(script_dir / "matrix_instances.txt"),
        help="Path to instances file (MXID password per line)",
    )
    parser.add_argument(
        "--rooms",
        default="",
        help="Path to rooms JSON (default: <instances_stem>_rooms.json)",
    )
    parser.add_argument(
        "--homeserver",
        default="",
        help="Override homeserver URL (default: value from rooms JSON)",
    )
    parser.add_argument(
        "--owner-password",
        default="",
        help="If set, verify owner login and membership in every room",
    )
    parser.add_argument(
        "--min-instances",
        type=int,
        default=4,
        help="Minimum number of bot accounts expected",
    )
    return parser.parse_args()


def _client_url(homeserver: str, path: str) -> str:
    return urljoin(homeserver.rstrip("/") + "/", path.lstrip("/"))


def _validate_mxid(mxid: str) -> None:
    if not mxid.startswith("@") or ":" not in mxid:
        raise ValueError(f"Invalid MXID '{mxid}' (expected @localpart:domain)")


def _validate_room_id(room_id: str) -> None:
    if not room_id.startswith("!"):
        raise ValueError(f"Invalid room ID '{room_id}' (expected !localpart:domain)")


def _load_instances(path: Path) -> list[tuple[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(f"Instances file not found: {path}")

    accounts: list[tuple[str, str]] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(f"{path}:{line_no}: expected 'MXID password', got '{raw}'")
        mxid, password = parts
        _validate_mxid(mxid)
        accounts.append((mxid, password))
    return accounts


def _rooms_path_for_instances(instances_path: Path, explicit: str) -> Path:
    if explicit:
        return Path(explicit).resolve()
    return instances_path.parent / f"{instances_path.stem}_rooms.json"


def _load_rooms(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Rooms file not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"{path}: expected JSON object, got {type(data).__name__}")

    homeserver = data.get("homeserver")
    owner = data.get("owner")
    rooms = data.get("rooms")

    if not isinstance(homeserver, str) or not homeserver.strip():
        raise ValueError(f"{path}: missing or invalid 'homeserver'")
    if not isinstance(owner, str):
        raise ValueError(f"{path}: missing or invalid 'owner'")
    _validate_mxid(owner)
    if not isinstance(rooms, dict) or not rooms:
        raise ValueError(f"{path}: missing or empty 'rooms'")

    for mxid, room_id in rooms.items():
        _validate_mxid(str(mxid))
        _validate_room_id(str(room_id))

    return data


def _check_cross_consistency(
    accounts: list[tuple[str, str]],
    rooms_data: dict[str, Any],
    *,
    min_instances: int,
) -> list[str]:
    errors: list[str] = []
    mxids = [mxid for mxid, _ in accounts]
    room_map: dict[str, str] = rooms_data["rooms"]

    if len(accounts) < min_instances:
        errors.append(f"instances file has {len(accounts)} bots, expected at least {min_instances}")

    if len(mxids) != len(set(mxids)):
        errors.append("instances file contains duplicate MXIDs")

    if len(room_map) != len(set(room_map.keys())):
        errors.append("rooms file contains duplicate bot MXIDs")

    if len(room_map) != len(set(room_map.values())):
        errors.append("rooms file maps multiple bots to the same room ID")

    account_set = set(mxids)
    room_set = set(room_map.keys())

    missing_rooms = sorted(account_set - room_set)
    extra_rooms = sorted(room_set - account_set)
    if missing_rooms:
        errors.append(f"bots in instances file missing from rooms: {', '.join(missing_rooms)}")
    if extra_rooms:
        errors.append(f"bots in rooms file missing from instances: {', '.join(extra_rooms)}")

    return errors


async def _api_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with session.request(method, url, headers=headers, json=body) as resp:
        data = await resp.json()
        if resp.status >= 400:
            raise MatrixApiError(resp.status, data)
        if not isinstance(data, dict):
            raise TypeError(f"Expected JSON object from {url}, got {type(data)}")
        return data


def _localpart(mxid: str) -> str:
    return mxid.split(":", 1)[0][1:]


async def _verify_homeserver(session: aiohttp.ClientSession, homeserver: str) -> None:
    url = _client_url(homeserver, "/_matrix/client/versions")
    async with session.get(url) as resp:
        if resp.status >= 400:
            body = await resp.text()
            raise RuntimeError(f"Homeserver unreachable ({resp.status}): {body[:200]}")
        data = await resp.json()
    versions = data.get("versions", [])
    if not versions:
        raise RuntimeError(f"Homeserver returned no Matrix versions: {data}")


async def _login(
    session: aiohttp.ClientSession,
    homeserver: str,
    mxid: str,
    password: str,
) -> dict[str, str]:
    data = await _api_json(
        session,
        "POST",
        _client_url(homeserver, "/_matrix/client/v3/login"),
        body={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": _localpart(mxid)},
            "password": password,
        },
    )
    user_id = data.get("user_id")
    access_token = data.get("access_token")
    if not user_id or not access_token:
        raise RuntimeError(f"Login response incomplete for {mxid}: {data}")
    return {"user_id": str(user_id), "access_token": str(access_token)}


async def _joined_rooms(
    session: aiohttp.ClientSession,
    homeserver: str,
    token: str,
) -> set[str]:
    data = await _api_json(
        session,
        "GET",
        _client_url(homeserver, "/_matrix/client/v3/joined_rooms"),
        token=token,
    )
    rooms = data.get("joined_rooms", [])
    return set(rooms) if isinstance(rooms, list) else set()


async def _room_state_ok(
    session: aiohttp.ClientSession,
    homeserver: str,
    token: str,
    room_id: str,
) -> None:
    path = f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/state"
    url = _client_url(homeserver, path)
    headers = {"Authorization": f"Bearer {token}"}
    async with session.get(url, headers=headers) as resp:
        if resp.status >= 400:
            data = await resp.json()
            if isinstance(data, dict):
                raise MatrixApiError(resp.status, data)
            raise MatrixApiError(resp.status, {"error": data})


async def _test_bot(
    session: aiohttp.ClientSession,
    homeserver: str,
    mxid: str,
    password: str,
    expected_room: str,
) -> list[str]:
    errors: list[str] = []
    label = mxid

    try:
        creds = await _login(session, homeserver, mxid, password)
    except MatrixApiError as exc:
        errors.append(f"{label}: login failed ({exc})")
        return errors
    except Exception as exc:
        errors.append(f"{label}: login failed ({exc})")
        return errors

    if creds["user_id"] != mxid:
        errors.append(f"{label}: logged in as {creds['user_id']}, expected {mxid}")

    joined = await _joined_rooms(session, homeserver, creds["access_token"])
    if expected_room not in joined:
        errors.append(f"{label}: not joined to expected room {expected_room}")

    try:
        await _room_state_ok(session, homeserver, creds["access_token"], expected_room)
    except MatrixApiError as exc:
        errors.append(f"{label}: cannot read state for room {expected_room} ({exc})")

    return errors


async def _test_owner(
    session: aiohttp.ClientSession,
    homeserver: str,
    owner_mxid: str,
    owner_password: str,
    room_map: dict[str, str],
) -> list[str]:
    errors: list[str] = []
    try:
        creds = await _login(session, homeserver, owner_mxid, owner_password)
    except Exception as exc:
        return [f"owner {owner_mxid}: login failed ({exc})"]

    if creds["user_id"] != owner_mxid:
        errors.append(f"owner: logged in as {creds['user_id']}, expected {owner_mxid}")

    joined = await _joined_rooms(session, homeserver, creds["access_token"])
    for bot_mxid, room_id in room_map.items():
        if room_id not in joined:
            errors.append(f"owner: not joined to room {room_id} for bot {bot_mxid}")
    return errors


async def _run_live_checks(
    homeserver: str,
    accounts: list[tuple[str, str]],
    rooms_data: dict[str, Any],
    *,
    owner_password: str,
) -> list[str]:
    errors: list[str] = []
    room_map: dict[str, str] = rooms_data["rooms"]

    async with aiohttp.ClientSession() as session:
        try:
            await _verify_homeserver(session, homeserver)
            print(f"  homeserver OK: {homeserver}")
        except Exception as exc:
            return [f"homeserver: {exc}"]

        for mxid, password in accounts:
            expected_room = room_map.get(mxid, "")
            if not expected_room:
                continue
            bot_errors = await _test_bot(session, homeserver, mxid, password, expected_room)
            if bot_errors:
                errors.extend(bot_errors)
            else:
                print(f"  bot OK: {mxid} -> {expected_room}")

        if owner_password:
            owner_errors = await _test_owner(
                session,
                homeserver,
                str(rooms_data["owner"]),
                owner_password,
                room_map,
            )
            if owner_errors:
                errors.extend(owner_errors)
            else:
                print(f"  owner OK: {rooms_data['owner']} in all {len(room_map)} rooms")

    return errors


def _print_header(title: str) -> None:
    print(f"\n== {title} ==")


async def _main() -> int:
    args = _parse_args()
    instances_path = Path(args.instances).resolve()
    rooms_path = _rooms_path_for_instances(instances_path, args.rooms)

    _print_header("load files")
    print(f"  instances: {instances_path}")
    print(f"  rooms:     {rooms_path}")

    file_errors: list[str] = []
    accounts: list[tuple[str, str]] = []
    rooms_data: dict[str, Any] = {}

    try:
        accounts = _load_instances(instances_path)
        print(f"  loaded {len(accounts)} bot account(s)")
    except Exception as exc:
        file_errors.append(f"instances: {exc}")

    try:
        rooms_data = _load_rooms(rooms_path)
        print(f"  loaded {len(rooms_data['rooms'])} room mapping(s)")
        print(f"  owner: {rooms_data['owner']}")
        print(f"  homeserver (from JSON): {rooms_data['homeserver']}")
    except Exception as exc:
        file_errors.append(f"rooms: {exc}")

    if file_errors:
        for err in file_errors:
            print(f"FAIL: {err}", file=sys.stderr)
        return 1

    _print_header("cross-file consistency")
    consistency_errors = _check_cross_consistency(
        accounts,
        rooms_data,
        min_instances=args.min_instances,
    )
    if consistency_errors:
        for err in consistency_errors:
            print(f"FAIL: {err}", file=sys.stderr)
        return 1
    print("  MXIDs and room mappings match")

    homeserver = args.homeserver.strip() or str(rooms_data["homeserver"])
    if args.homeserver and args.homeserver.rstrip("/") != str(rooms_data["homeserver"]).rstrip("/"):
        print(
            f"  note: using --homeserver {homeserver} "
            f"(JSON has {rooms_data['homeserver']})",
            file=sys.stderr,
        )

    _print_header("live Matrix checks")
    live_errors = await _run_live_checks(
        homeserver,
        accounts,
        rooms_data,
        owner_password=args.owner_password,
    )
    if live_errors:
        for err in live_errors:
            print(f"FAIL: {err}", file=sys.stderr)
        return 1

    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
