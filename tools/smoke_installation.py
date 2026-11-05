"""Run a GEO mission from a clean, non-editable wheel installation on both engines."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

import gmat_mcp
from gmat_mcp.config import load_config
from gmat_mcp.core import Manager


async def smoke(args):
    package = Path(gmat_mcp.__file__).resolve()
    if not package.is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError(f"Expected installed wheel inside {sys.prefix}, got {package}")
    config = load_config(args.config)
    config.server.data_dir = (args.output.parent / "wheel-data").resolve()
    results = {"python": sys.version, "package": str(package), "profiles": {}}
    manager = Manager(config)
    try:
        for name in config.profiles:
            opened = await manager.create_session(name)
            sid = opened["session_id"]
            await manager.load_script(sid, path=str(args.mission.resolve()))
            run = await manager.run_mission(sid)
            async with asyncio.timeout(120):
                while run["status"] in ("queued", "running"):
                    await asyncio.sleep(0.05)
                    run = manager.get_run(run["run_id"])
            if run["status"] != "succeeded":
                raise RuntimeError(f"{name}: {run}")
            state = await manager.call(
                sid, "get_object", name="AmericasGEO", fields=["X", "Y", "Z"], view="runtime"
            )
            directory = Path(run["directory"])
            provenance = json.loads((directory / "provenance.json").read_text())
            startup_hash = hashlib.sha256(
                (directory / "effective_startup.txt").read_bytes()
            ).hexdigest()
            mission_hash = hashlib.sha256((directory / "mission.script").read_bytes()).hexdigest()
            if (
                provenance["startup_sha256"] != startup_hash
                or provenance["mission_sha256"] != mission_hash
            ):
                raise RuntimeError(f"{name}: saved-byte provenance hashes do not match")
            results["profiles"][name] = {
                "run_id": run["run_id"],
                "status": run["status"],
                "state": state,
                "provenance": provenance,
                "startup_sha256": startup_hash,
            }
            await manager.close_session(sid)
    finally:
        await manager.shutdown()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps({name: r["status"] for name, r in results["profiles"].items()}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--mission", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(smoke(parser.parse_args()))
