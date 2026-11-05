from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from platformdirs import user_config_path

from .config import load_config
from .core import Manager
from .discovery import discover, write_config
from .errors import GmatError


async def doctor(manager: Manager, profile_ids: list[str]) -> int:
    results = []
    try:
        for profile_id in profile_ids or list(manager.config.profiles):
            try:
                session = await manager.create_session(profile_id)
                results.append({"profile_id": profile_id, "ok": True, "engine": session["engine"]})
                await manager.close_session(session["session_id"])
            except GmatError as exc:
                results.append({"profile_id": profile_id, "ok": False, "error": exc.as_dict()})
    finally:
        await manager.shutdown()
    print(json.dumps({"profiles": results}, indent=2))
    return 0 if results and all(item["ok"] for item in results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Interactive MCP sessions for NASA GMAT and GMAT-Qt"
    )
    parser.add_argument(
        "--config", type=Path, help="TOML installation profiles (or GMAT_MCP_CONFIG)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("discover", "init"):
        setup = commands.add_parser(
            name,
            help="Find local GMAT installations and matching interpreters"
            if name == "discover"
            else "Generate installation profiles without modifying GMAT",
        )
        setup.add_argument("--root", type=Path, action="append", default=[])
        setup.add_argument("--python", action="append", default=[])
        if name == "init":
            setup.add_argument("--output", type=Path, default=None)
            setup.add_argument("--overwrite", action="store_true")
    serve = commands.add_parser("serve", help="Run the MCP server")
    serve.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--token-env",
        default="GMAT_MCP_TOKEN",
        help="Environment variable holding the HTTP bearer token",
    )
    diagnostic = commands.add_parser("doctor", help="Probe configured engines in isolated workers")
    diagnostic.add_argument("profiles", nargs="*")
    args = parser.parse_args()
    try:
        if args.command in ("discover", "init"):
            report = discover(args.root, args.python)
            if args.command == "init":
                destination = (
                    args.output
                    or args.config
                    or Path(
                        os.environ.get(
                            "GMAT_MCP_CONFIG",
                            user_config_path("gmat-mcp", appauthor=False) / "config.toml",
                        )
                    )
                )
                report = write_config(report, destination, args.overwrite)
            print(json.dumps(report, indent=2))
            return 0
        manager = Manager(load_config(args.config))
        if args.command == "doctor":
            return asyncio.run(doctor(manager, args.profiles))
        from .server import LocalAuth, create_server

        server = create_server(manager)
        if args.transport == "stdio":
            server.run(transport="stdio")
        else:
            import uvicorn

            token = os.environ.get(args.token_env)
            if not token:
                raise GmatError(
                    "CONFIG_ERROR", f"Set {args.token_env} to a bearer token for local HTTP"
                )
            if not 1 <= args.port <= 65535:
                raise GmatError("CONFIG_ERROR", "port must be 1..65535")
            app = LocalAuth(server.streamable_http_app(), token, args.port)
            uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
        return 0
    except (GmatError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
