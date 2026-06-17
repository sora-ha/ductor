#!/usr/bin/env python3
"""Register Matrix accounts for ductor instances on an open-registration server.

The script creates N bot accounts, writes an instances file for
launch_4_kimi_matrix.sh, and prints a ready-to-run launcher command.

Usage:
    python3 scripts/register_matrix_bots.py \
        --homeserver https://matrix.example.com \
        --base-username ductor-kimi \
        --password VerySecret123 \
        --allowed-user @you:matrix.example.com \
        --count 4 \
        --output scripts/matrix_instances.txt

Then launch the bots:
    ./scripts/launch_4_kimi_matrix.sh https://matrix.example.com @you:matrix.example.com scripts/matrix_instances.txt
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import urljoin

import aiohttp


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Register Matrix bot accounts for ductor instances."
    )
    parser.add_argument("--homeserver", required=True, help="Matrix homeserver URL")
    parser.add_argument("--base-username", required=True, help="Base localpart, e.g. ductor-kimi")
    parser.add_argument("--password", required=True, help="Password for each bot")
    parser.add_argument("--allowed-user", required=True, help="MXID allowed to talk to the bots")
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
    return parser.parse_args()


async def _register(  # noqa: C901, PLR0912
    session: aiohttp.ClientSession,
    homeserver: str,
    username: str,
    password: str,
) -> str:
    """Register a Matrix user and return the full MXID."""
    register_url = urljoin(homeserver.rstrip("/") + "/", "_matrix/client/v3/register")
    body: dict = {"username": username, "password": password}

    # Some servers want the initial request without auth; others accept the
    # whole flow up front.  We iterate through authentication stages.
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

            # Pick the first flow we know how to complete.
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

            # Find the next uncompleted stage in the chosen flow.
            next_stage = None
            for stage in chosen_flow:
                if stage not in completed:
                    next_stage = stage
                    break

            if next_stage is None:
                # All stages reported complete but we did not get 200; bail.
                raise RuntimeError(f"Auth stages complete but registration not finished: {data}")

            auth = {"type": next_stage, "session": session_id}
            body["auth"] = auth
            continue

        raise RuntimeError(f"Unexpected registration response {resp.status}: {data}")

    raise RuntimeError(f"Could not complete registration for '{username}' after {attempts} attempts")


async def _main() -> int:
    args = _parse_args()

    accounts: list[tuple[str, str]] = []
    failed: list[tuple[str, Exception]] = []

    async with aiohttp.ClientSession() as session:
        for i in range(args.start_from, args.start_from + args.count):
            username = f"{args.base_username}-{i}"
            print(f"Registering @{username} ...", flush=True)
            try:
                mxid = await _register(session, args.homeserver, username, args.password)
                print(f"  -> {mxid}", flush=True)
                accounts.append((mxid, args.password))
            except Exception as exc:
                print(f"  -> FAILED: {exc}", flush=True)
                failed.append((username, exc))

    if len(accounts) < args.count:
        print(f"\nOnly {len(accounts)}/{args.count} accounts registered.", file=sys.stderr)
        for username, exc in failed:
            print(f"  - {username}: {exc}", file=sys.stderr)
        if not accounts:
            return 1

    output_path = _write_instances_file(args.output, accounts)
    print(f"\nWrote {len(accounts)} accounts to {output_path}")
    print("\nLaunch the instances with:")
    print(
        f"  ./scripts/launch_4_kimi_matrix.sh {args.homeserver} {args.allowed_user} {output_path}"
    )
    return 0


def _write_instances_file(output: str, accounts: list[tuple[str, str]]) -> Path:
    output_path = Path(output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "\n".join(f"{mxid} {password}" for mxid, password in accounts) + "\n",
        encoding="utf-8",
    )
    return output_path


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
