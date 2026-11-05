"""Disposable GUI instances only. MCP detachment must preserve the GUI process."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from gmat_mcp.installation import desktop_executable

from .protocol_support import call, connected, wait_run, write_config

pytestmark = pytest.mark.native
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_live_desktop_bridge(native_config, tmp_path, transport):
    if sys.platform == "linux" and not shutil.which("Xvfb"):
        pytest.skip("Linux native desktop qualification requires Xvfb")
    config = native_config.model_copy(deep=True)
    profile_id = next(n for n, p in config.profiles.items() if p.variant == "qt")
    profile = config.profiles[profile_id]
    app = desktop_executable(profile.root, windows=sys.platform == "win32")
    config.server.data_dir = tmp_path / "data"
    config.server.desktop_discovery_dir = tmp_path / "desktops"
    path = tmp_path / "profiles.toml"
    write_config(config, path)
    # Use an isolated display and settings; never attach to the user's application.
    with (tmp_path / "xvfb.log").open("w") as xlog, (tmp_path / "gui.log").open("w") as log:
        xvfb = None
        environment = {**os.environ, "LIBGL_ALWAYS_SOFTWARE": "1"}
        if sys.platform == "linux":
            xvfb = subprocess.Popen(
                ["Xvfb", "-displayfd", "1", "-screen", "0", "1400x1000x24"],
                stdout=subprocess.PIPE,
                stderr=xlog,
                text=True,
            )
            environment["DISPLAY"] = ":" + xvfb.stdout.readline().strip()
        else:
            environment["QT_QPA_PLATFORM"] = "offscreen"
            environment["QT_MAC_DISABLE_FOREGROUND_APPLICATION_TRANSFORM"] = "1"
        gui = subprocess.Popen(
            [
                str(app),
                "--mcp-bridge",
                "--mcp-discovery-dir",
                str(config.server.desktop_discovery_dir),
                "--settings-dir",
                str(tmp_path / "settings"),
                str(ROOT / "missions/AmericasGEO_15deg_90W.script"),
            ],
            env=environment,
            stdout=log,
            stderr=log,
        )
        try:
            async with connected(transport, path, tmp_path) as client:
                async with asyncio.timeout(30):
                    while True:
                        discovered = await call(client, "list_desktops")
                        if discovered["desktops"]:
                            break
                        assert gui.poll() is None, (tmp_path / "gui.log").read_text()
                        await asyncio.sleep(0.1)
                desktop = discovered["desktops"][0]
                assert "token" not in str(desktop)
                session = await call(
                    client,
                    "attach_desktop",
                    desktop_id=desktop["desktop_id"],
                    profile_id=profile_id,
                )
                sid = session["session_id"]
                assert session["backend"] == "desktop"
                changes = [
                    {"op": "set_field", "name": "AmericasGEO", "field": "DryMass", "value": 555}
                ]
                preview = await call(
                    client,
                    "edit_mission",
                    session_id=sid,
                    expected_version=session["model_version"],
                    changes=changes,
                    dry_run=True,
                )
                assert not preview["committed"]
                changed = await call(
                    client,
                    "edit_mission",
                    session_id=sid,
                    expected_version=session["model_version"],
                    changes=changes,
                )
                stale = await client.call_tool(
                    "edit_mission",
                    {
                        "session_id": sid,
                        "expected_version": session["model_version"],
                        "changes": changes,
                    },
                )
                assert stale.is_error
                obj = await call(
                    client, "get_object", session_id=sid, name="AmericasGEO", fields=["DryMass"]
                )
                assert obj["fields"][0]["value"] == 555
                configured = await call(
                    client,
                    "configure_results",
                    session_id=sid,
                    expected_version=changed["model_version"],
                    spec={
                        "datasets": [
                            {
                                "id": "geo",
                                "spacecraft": "AmericasGEO",
                                "frame": "EarthMJ2000Eq",
                                "body": "Earth",
                                "groundtrack": True,
                            }
                        ]
                    },
                )
                assert configured["committed"]
                run = await wait_run(client, await call(client, "run_mission", session_id=sid))
                assert run["status"] == "succeeded", run
                datasets = (await call(client, "list_datasets", run_id=run["run_id"]))["datasets"]
                assert datasets[0]["complete"] and datasets[0]["rows"] >= 865, datasets
                latest = await call(client, "get_session", session_id=sid)
                assert not latest["desktop"]["running"]
                assert latest["desktop"]["run"]["run_id"] == run["run_id"]
                long_text = (
                    (ROOT / "missions/AmericasGEO_15deg_90W.script")
                    .read_text()
                    .replace("1:1:864", "1:1:100000000")
                )
                await call(
                    client,
                    "load_script",
                    session_id=sid,
                    text=long_text,
                    project_dir=str(ROOT / "missions"),
                    expected_version=latest["model_version"],
                )
                running = await call(client, "run_mission", session_id=sid)
                async with asyncio.timeout(10):
                    while True:
                        state = await call(client, "get_session", session_id=sid)
                        if state["desktop"]["running"]:
                            break
                        await asyncio.sleep(0.02)
                busy = await client.call_tool(
                    "get_object", {"session_id": sid, "name": "AmericasGEO"}
                )
                assert busy.is_error
                await call(client, "pause_run", run_id=running["run_id"])
                paused = await call(client, "get_session", session_id=sid)
                assert paused["desktop"]["paused"]
                await call(client, "resume_run", run_id=running["run_id"])
                logs = await call(client, "get_logs", session_id=sid)
                assert logs["next_offset"] > 0
                cancelled = await call(client, "cancel_run", run_id=running["run_id"])
                assert cancelled["status"] == "cancelled"
                await call(client, "detach_desktop", session_id=sid)
                assert gui.poll() is None
        finally:
            # Harness cleanup of its own disposable application. The MCP adapter
            # has no terminate/kill operation and was already detached above.
            gui.terminate()
            gui.wait(timeout=10)
            if xvfb is not None:
                xvfb.terminate()
                xvfb.wait(timeout=10)
