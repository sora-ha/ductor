#!/usr/bin/env python3
"""Register Matrix accounts for ductor instances on an open-registration server.

The script verifies the owner account first, creates N bot accounts, provisions
a private DM room with the owner for each bot, writes an instances file for
launch_4_kimi_matrix.sh, and verifies every room ID before exiting.

Usage:
    python3 scripts/register_matrix_bots.py \
        --homeserver https://matrix.example.com \
        --base-username ductor-kimi \
        --password VerySecret123 \
        --allowed-user @you:matrix.example.com \
        --owner-password your-owner-password \
        --count 4 \
        --output scripts/matrix_instances.txt

Then launch the bots:
    ./scripts/launch_4_kimi_matrix.sh https://matrix.example.com @you:matrix.example.com scripts/matrix_instances.txt
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
    parser = argparse.ArgumentParser(
        description="Register Matrix bot accounts for ductor instances."
    )
    parser.add_argument("--homeserver", required=True, help="Matrix homeserver URL")
    parser.add_argument("--base-username", required=True, help="Base localpart, e.g. ductor-kimi")
    parser.add_argument("--password", required=True, help="Password for each bot")
    parser.add_argument("--allowed-user", required=True, help="Owner MXID allowed to talk to bots")
    parser.add_argument(
        "--owner-password",
        required=True,
        help="Password for the owner account (--allowed-user)",
    )
    parser.add_argument("--count", type=int, default=4, help="Number of bots to register")
    parser.add_argument(
        "--output",
        default="scripts/matrix_instances.txt",
        help="Path to write the instances file",
    )
    parser.add_argument(
        "--start-from",
        type=int,
        default=1,
        help="Start numbering from this value (useful if re-running after partial failure)",
    )
    parser.add_argument(
        "--skip-rooms",
        action="store_true",
        help="Register accounts only; do not create owner DM rooms",
    )
    args = parser.parse_args()
    _validate_mxid(args.allowed_user)
    return args


def _client_url(homeserver: str, path: str) -> str:
    return urljoin(homeserver.rstrip("/") + "/", path.lstrip("/"))


def _localpart(mxid: str) -> str:
    if not mxid.startswith("@"):
        return mxid
    return mxid.split(":", 1)[0][1:]


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


def _validate_mxid(mxid: str) -> None:
    if not mxid.startswith("@") or ":" not in mxid:
        raise ValueError(f"Invalid MXID '{mxid}' (expected @localpart:domain)")


async def _verify_homeserver(session: aiohttp.ClientSession, homeserver: str) -> None:
    """Confirm the homeserver is reachable and speaks Matrix."""
    url = _client_url(homeserver, "/_matrix/client/versions")
    async with session.get(url) as resp:
        if resp.status >= 400:
            body = await resp.text()
            raise RuntimeError(f"Homeserver unreachable ({resp.status}): {body[:200]}")
        data = await resp.json()
    versions = data.get("versions", [])
    if not versions:
        raise RuntimeError(f"Homeserver returned no Matrix versions: {data}")
    print(f"  homeserver OK (Matrix {versions[0]})", flush=True)


async def _verify_owner(
    session: aiohttp.ClientSession,
    homeserver: str,
    owner_mxid: str,
    owner_password: str,
) -> dict[str, str]:
    """Login as owner and confirm MXID + credentials before any bot work."""
    _validate_mxid(owner_mxid)
    print(f"Verifying owner {owner_mxid} ...", flush=True)
    await _verify_homeserver(session, homeserver)

    try:
        creds = await _login(session, homeserver, owner_mxid, owner_password)
    except MatrixApiError as exc:
        if exc.data.get("errcode") in {"M_FORBIDDEN", "M_USER_DEACTIVATED"}:
            raise RuntimeError(
                f"Owner login failed for {owner_mxid}: wrong password or account disabled"
            ) from exc
        raise RuntimeError(f"Owner login failed for {owner_mxid}: {exc}") from exc

    whoami = await _api_json(
        session,
        "GET",
        _client_url(homeserver, "/_matrix/client/v3/account/whoami"),
        token=creds["access_token"],
    )
    logged_in_as = whoami.get("user_id")
    if logged_in_as != owner_mxid:
        raise RuntimeError(
            f"Owner MXID mismatch: --allowed-user is {owner_mxid} "
            f"but login resolved to {logged_in_as}"
        )

    print(f"  owner verified: {logged_in_as}", flush=True)
    return creds


async def _login(
    session: aiohttp.ClientSession,
    homeserver: str,
    user: str,
    password: str,
) -> dict[str, str]:
    """Login and return user_id, access_token, device_id."""
    localpart = _localpart(user)
    data = await _api_json(
        session,
        "POST",
        _client_url(homeserver, "/_matrix/client/v3/login"),
        body={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": localpart},
            "password": password,
        },
    )
    user_id = data.get("user_id")
    access_token = data.get("access_token")
    device_id = data.get("device_id", "")
    if not user_id or not access_token:
        raise RuntimeError(f"Login succeeded but response incomplete: {data}")
    return {
        "user_id": user_id,
        "access_token": access_token,
        "device_id": device_id,
    }


async def _register(  # noqa: C901, PLR0912
    session: aiohttp.ClientSession,
    homeserver: str,
    username: str,
    password: str,
) -> str:
    """Register a Matrix user and return the full MXID."""
    register_url = _client_url(homeserver, "/_matrix/client/v3/register")
    body: dict[str, Any] = {"username": username, "password": password}

    attempts = 0
    while attempts < 10:
        attempts += 1
        async with session.post(register_url, params={"kind": "user"}, json=body) as resp:
            data = await resp.json()

        if resp.status == 200:
            mxid = data.get("user_id")
            if not mxid:
                raise RuntimeError(f"Registration succeeded but no user_id returned: {data}")
            return mxid

        if resp.status == 400:
            errcode = data.get("errcode")
            if errcode == "M_USER_IN_USE":
                raise RuntimeError(f"Username '{username}' is already taken")
            if errcode == "M_EXCLUSIVE":
                raise RuntimeError(f"Username '{username}' is reserved/exclusive")
            raise RuntimeError(f"Registration failed: {errcode} - {data.get('error')}")

        if resp.status == 401:
            session_id = data.get("session")
            flows = data.get("flows", [])
            completed = set(data.get("completed", []))

            if not session_id or not flows:
                raise RuntimeError(f"Registration requires unsupported auth: {data}")

            chosen_flow = None
            for flow in flows:
                stages = flow.get("stages", [])
                if all(stage in {"m.login.dummy", "m.login.terms"} for stage in stages):
                    chosen_flow = stages
                    break

            if chosen_flow is None:
                raise RuntimeError(
                    f"Registration requires unsupported auth flow. Available flows: {flows}"
                )

            next_stage = None
            for stage in chosen_flow:
                if stage not in completed:
                    next_stage = stage
                    break

            if next_stage is None:
                raise RuntimeError(f"Auth stages complete but registration not finished: {data}")

            auth = {"type": next_stage, "session": session_id}
            body["auth"] = auth
            continue

        raise RuntimeError(f"Unexpected registration response {resp.status}: {data}")

    raise RuntimeError(f"Could not complete registration for '{username}' after {attempts} attempts")


async def _register_or_login(
    session: aiohttp.ClientSession,
    homeserver: str,
    username: str,
    password: str,
) -> str:
    try:
        return await _register(session, homeserver, username, password)
    except RuntimeError as exc:
        if "already taken" not in str(exc):
            raise
        print("  -> account exists, logging in ...", flush=True)
        creds = await _login(session, homeserver, username, password)
        return creds["user_id"]


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


async def _direct_room_for_bot(
    session: aiohttp.ClientSession,
    homeserver: str,
    owner_token: str,
    owner_mxid: str,
    bot_mxid: str,
) -> str | None:
    path = (
        f"/_matrix/client/v3/user/{quote(owner_mxid, safe='')}/account_data/m.direct"
    )
    try:
        data = await _api_json(
            session,
            "GET",
            _client_url(homeserver, path),
            token=owner_token,
        )
    except MatrixApiError as exc:
        if exc.data.get("errcode") == "M_NOT_FOUND":
            return None
        raise
    rooms = data.get(bot_mxid)
    if isinstance(rooms, list) and rooms:
        return str(rooms[0])
    return None


async def _create_dm_room(
    session: aiohttp.ClientSession,
    homeserver: str,
    owner_token: str,
    bot_mxid: str,
) -> str:
    data = await _api_json(
        session,
        "POST",
        _client_url(homeserver, "/_matrix/client/v3/createRoom"),
        token=owner_token,
        body={
            "preset": "trusted_private_chat",
            "is_direct": True,
            "invite": [bot_mxid],
            "name": f"DM: {bot_mxid}",
        },
    )
    room_id = data.get("room_id")
    if not room_id:
        raise RuntimeError(f"createRoom succeeded but no room_id returned: {data}")
    return str(room_id)


async def _join_room(
    session: aiohttp.ClientSession,
    homeserver: str,
    token: str,
    room_id: str,
) -> None:
    path = f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/join"
    await _api_json(session, "POST", _client_url(homeserver, path), token=token, body={})


async def _ensure_owner_dm_room(
    session: aiohttp.ClientSession,
    homeserver: str,
    owner_creds: dict[str, str],
    bot_mxid: str,
    bot_password: str,
) -> str:
    owner_token = owner_creds["access_token"]
    owner_mxid = owner_creds["user_id"]

    room_id = await _direct_room_for_bot(session, homeserver, owner_token, owner_mxid, bot_mxid)
    if room_id:
        print(f"  -> reusing existing DM room {room_id}", flush=True)
    else:
        room_id = await _create_dm_room(session, homeserver, owner_token, bot_mxid)
        print(f"  -> created DM room {room_id}", flush=True)

    bot_creds = await _login(session, homeserver, bot_mxid, bot_password)
    bot_joined = await _joined_rooms(session, homeserver, bot_creds["access_token"])
    if room_id not in bot_joined:
        print("  -> bot joining room ...", flush=True)
        await _join_room(session, homeserver, bot_creds["access_token"], room_id)

    owner_joined = await _joined_rooms(session, homeserver, owner_token)
    if room_id not in owner_joined:
        raise RuntimeError(f"Owner is not in room {room_id}")

    bot_joined = await _joined_rooms(session, homeserver, bot_creds["access_token"])
    if room_id not in bot_joined:
        raise RuntimeError(f"Bot {bot_mxid} failed to join room {room_id}")

    return room_id


async def _provision_bot(
    session: aiohttp.ClientSession,
    args: argparse.Namespace,
    owner_creds: dict[str, str],
    index: int,
) -> tuple[str, str, str | None]:
    username = f"{args.base_username}-{index}"
    print(f"Registering @{username} ...", flush=True)
    mxid = await _register_or_login(session, args.homeserver, username, args.password)
    print(f"  -> {mxid}", flush=True)

    room_id: str | None = None
    if not args.skip_rooms:
        print("  -> ensuring owner DM room ...", flush=True)
        room_id = await _ensure_owner_dm_room(
            session,
            args.homeserver,
            owner_creds,
            mxid,
            args.password,
        )
        print(f"  -> verified room {room_id}", flush=True)
    return mxid, args.password, room_id


def _finalize_outputs(
    args: argparse.Namespace,
    accounts: list[tuple[str, str]],
    rooms: dict[str, str],
    failed: list[tuple[str, Exception]],
) -> int:
    if len(accounts) < args.count:
        print(f"\nOnly {len(accounts)}/{args.count} accounts registered.", file=sys.stderr)
        for username, exc in failed:
            print(f"  - {username}: {exc}", file=sys.stderr)
        if not accounts:
            return 1

    output_path = _write_instances_file(args.output, accounts)
    print(f"\nWrote {len(accounts)} accounts to {output_path}")

    if not args.skip_rooms:
        if len(rooms) != len(accounts):
            print(
                f"ERROR: expected {len(accounts)} room IDs, got {len(rooms)}",
                file=sys.stderr,
            )
            return 1
        rooms_path = _write_rooms_file(args.output, args.homeserver, args.allowed_user, rooms)
        print(f"Wrote {len(rooms)} room IDs to {rooms_path}")

    print("\nLaunch the instances with:")
    print(
        f"  ./scripts/launch_4_kimi_matrix.sh {args.homeserver} {args.allowed_user} {output_path}"
    )
    return 0


async def _main() -> int:
    args = _parse_args()

    accounts: list[tuple[str, str]] = []
    rooms: dict[str, str] = {}
    failed: list[tuple[str, Exception]] = []

    async with aiohttp.ClientSession() as session:
        try:
            owner_creds = await _verify_owner(
                session,
                args.homeserver,
                args.allowed_user,
                args.owner_password,
            )
        except Exception as exc:
            print(f"Owner verification failed: {exc}", file=sys.stderr)
            return 1

        for i in range(args.start_from, args.start_from + args.count):
            try:
                mxid, password, room_id = await _provision_bot(session, args, owner_creds, i)
                accounts.append((mxid, password))
                if room_id:
                    rooms[mxid] = room_id
            except Exception as exc:
                username = f"{args.base_username}-{i}"
                print(f"  -> FAILED: {exc}", flush=True)
                failed.append((username, exc))

    return _finalize_outputs(args, accounts, rooms, failed)


def _write_instances_file(output: str, accounts: list[tuple[str, str]]) -> Path:
    output_path = Path(output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "\n".join(f"{mxid} {password}" for mxid, password in accounts) + "\n",
        encoding="utf-8",
    )
    return output_path


def _write_rooms_file(
    instances_output: str,
    homeserver: str,
    owner_mxid: str,
    rooms: dict[str, str],
) -> Path:
    output_path = Path(instances_output).resolve()
    rooms_path = output_path.parent / f"{output_path.stem}_rooms.json"
    payload = {
        "homeserver": homeserver,
        "owner": owner_mxid,
        "rooms": rooms,
    }
    rooms_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return rooms_path


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
