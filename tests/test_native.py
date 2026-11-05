from __future__ import annotations

import asyncio
import hashlib
import json
import math
import shutil
from pathlib import Path

import pytest

from gmat_mcp.core import Manager
from gmat_mcp.errors import GmatError
from gmat_mcp.process import prepare_loader, worker_environment
from gmat_mcp.startup import prepare_startup

from .protocol_support import call, connected, wait_run, write_config
from .test_core import finished

pytestmark = pytest.mark.native
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
STATE = ["X", "Y", "Z", "VX", "VY", "VZ"]


@pytest.fixture(scope="module")
def baseline_results():
    return {}


async def direct(profile, script, directory):
    directory.mkdir()
    loader, aliases = prepare_loader(profile.bin_dir, directory)
    startup, _ = prepare_startup(profile.startup, profile.bin_dir, directory, aliases)
    destination = directory / "result.json"
    with (directory / "native.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            profile.python_executable,
            "-B",
            str(Path(__file__).with_name("direct_api.py")),
            str(profile.bin_dir),
            str(startup),
            str(script),
            str(destination),
            env=worker_environment(profile.bin_dir, loader),
            cwd=directory,
            stdout=log,
            stderr=log,
        )
        await asyncio.wait_for(process.wait(), 60)
    assert process.returncode == 0, (directory / "native.log").read_text()
    return json.loads(destination.read_text())


@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_native_workflows_both_backends(native_config, tmp_path, transport, baseline_results):
    config = native_config.model_copy(deep=True)
    config.server.data_dir = tmp_path / "artifacts"
    path = tmp_path / "profiles.toml"
    write_config(config, path)
    # Installed startup files must not be changed by any MCP operation.
    originals = {
        name: hashlib.sha256(p.startup.read_bytes()).hexdigest()
        for name, p in config.profiles.items()
    }
    async with connected(transport, path, tmp_path) as client:
        for profile_id, profile in config.profiles.items():
            sid = (await call(client, "create_session", profile_id=profile_id))["session_id"]
            for name in ("propagation", "maneuver", "targeting"):
                script = EXAMPLES / f"{name}.script"
                key = (profile_id, name)
                if key not in baseline_results:
                    baseline_results[key] = await direct(
                        profile, script, tmp_path / f"direct-{profile_id}-{name}"
                    )
                baseline = baseline_results[key]
                assert baseline["native_status"] == 1
                await call(client, "load_script", session_id=sid, path=str(script))
                configured = await call(
                    client, "get_object", session_id=sid, name="Sat", fields=STATE
                )
                await call(client, "validate_mission", session_id=sid)
                run = await wait_run(client, await call(client, "run_mission", session_id=sid))
                assert run["status"] == "succeeded", run
                result = await call(
                    client, "get_object", session_id=sid, name="Sat", fields=STATE, view="runtime"
                )
                values = [field["value"] for field in result["fields"]]
                assert values == pytest.approx(baseline["state"], rel=1e-12, abs=1e-9)
                assert result["context"]["Epoch"] == baseline["epoch"]
                assert values != [f["value"] for f in configured["fields"]]
                assert result["run_id"] == run["run_id"]
                assert run["summary"] == baseline["summary"]
                if name == "targeting":
                    assert abs(math.sqrt(sum(x * x for x in values[:3])) - 8000) < 0.01
                artifacts = (await call(client, "list_artifacts", run_id=run["run_id"]))[
                    "artifacts"
                ]
                report = next(a for a in artifacts if a["path"] == f"output/{name}.txt")
                body = (
                    await call(
                        client, "read_artifact", run_id=run["run_id"], artifact_id=report["id"]
                    )
                )["content"]
                row = body.splitlines()[-1].split()
                assert [float(v) for v in row[-6:]] == pytest.approx(values, rel=1e-12, abs=1e-9)
                assert {
                    "mission.script",
                    "effective_startup.txt",
                    "provenance.json",
                    "run.json",
                } <= {a["path"] for a in artifacts}
            await call(client, "close_session", session_id=sid)
    for name, p in config.profiles.items():
        assert hashlib.sha256(p.startup.read_bytes()).hexdigest() == originals[name]


async def test_native_interactive_api_exchange_and_recovery(native_config, tmp_path):
    config = native_config.model_copy(deep=True)
    config.server.data_dir = tmp_path / "artifacts"
    manager = Manager(config)
    try:
        sessions = await asyncio.gather(*(manager.create_session(name) for name in config.profiles))
        for session in sessions:
            sid = session["session_id"]
            await manager.call(sid, "create_object", mutates=True, type="Spacecraft", name="ApiSat")
            await manager.call(
                sid, "set_field", mutates=True, name="ApiSat", field="DryMass", value=200.0
            )
            await manager.call(
                sid, "create_object", mutates=True, type="Propagator", name="ApiProp"
            )
            await manager.call(
                sid,
                "append_command",
                mutates=True,
                type="Propagate",
                description="ApiProp(ApiSat) {ApiSat.ElapsedSecs = 60}",
            )
            await manager.call(sid, "validate")
            assert (await manager.call(sid, "list_commands"))["commands"]
            assert "ApiSat" in (await manager.call(sid, "list_objects"))["objects"]
            run = await finished(manager, await manager.run_mission(sid))
            assert run["status"] == "succeeded", run
            assert (
                await manager.call(sid, "get_object", name="ApiSat", fields=["X"], view="runtime")
            )["run_id"] == run["run_id"]
            before = await manager.export_script(sid)
            with pytest.raises(GmatError):
                await manager.load_script(sid, text="Create NotAClass Nope;\n")

            def model_lines(text):
                # GMAT regenerates the export timestamp even for an unchanged model.
                return [line for line in text.splitlines() if not line.startswith("%Created:")]

            assert model_lines((await manager.export_script(sid))["text"]) == model_lines(
                before["text"]
            )
            with pytest.raises(GmatError):
                await manager.call(
                    sid, "set_field", mutates=True, name="ApiSat", field="NotAField", value=0.0
                )
            # Repeated execution after configuration edits.
            await manager.call(
                sid, "set_field", mutates=True, name="ApiSat", field="DryMass", value=210.0
            )
            with pytest.raises(GmatError):
                await manager.call(sid, "get_object", name="ApiSat", fields=["X"], view="runtime")
            assert (await finished(manager, await manager.run_mission(sid)))[
                "status"
            ] == "succeeded"
        exported = await manager.export_script(sessions[0]["session_id"])
        await manager.load_script(sessions[1]["session_id"], text=exported["text"])
        run = await finished(manager, await manager.run_mission(sessions[1]["session_id"]))
        assert run["status"] == "succeeded", run
    finally:
        workers = [session.worker for session in manager.sessions.values()]
        await manager.shutdown()
        assert all(worker.process.returncode is not None for worker in workers)


async def test_native_explicit_project_directory_for_relative_inputs(native_config, tmp_path):
    config = native_config.model_copy(deep=True)
    config.server.data_dir = tmp_path / "artifacts"
    manager = Manager(config)
    try:
        for profile_id, profile in config.profiles.items():
            project = tmp_path / profile_id / "project files"
            project.mkdir(parents=True)
            shutil.copyfile(profile.data_dir / "gravity/earth/JGM3.cof", project / "custom.cof")
            (project / "fragment.script").write_text(
                "Create Variable IncludedValue;\nGMAT IncludedValue = 42;\n"
            )
            text = (
                (EXAMPLES / "propagation.script")
                .read_text()
                .replace(
                    "GMAT FM.PrimaryBodies = {};\nGMAT FM.PointMasses = {Earth};",
                    "GMAT FM.PrimaryBodies = {Earth};\nGMAT FM.PointMasses = {};\n"
                    "GMAT FM.GravityField.Earth.Degree = 2;\n"
                    "GMAT FM.GravityField.Earth.Order = 2;\n"
                    "GMAT FM.GravityField.Earth.PotentialFile = 'custom.cof';",
                )
            )
            text = "#Include 'fragment.script'\n" + text
            sid = (await manager.create_session(profile_id))["session_id"]
            external_script = tmp_path / f"{profile_id}.script"
            external_script.write_text(text)
            for arguments in ({"text": text}, {"path": str(external_script)}):
                await manager.load_script(sid, project_dir=str(project), **arguments)
                assert "IncludedValue" in (await manager.call(sid, "list_objects"))["objects"]
                await manager.call(sid, "validate")
                run = await finished(manager, await manager.run_mission(sid))
                assert run["status"] == "succeeded", run
            await manager.close_session(sid)
    finally:
        await manager.shutdown()
