from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import pytest

from gmat_mcp.config import load_config
from gmat_mcp.core import Manager
from gmat_mcp.errors import GmatError
from gmat_mcp.process import prepare_loader
from gmat_mcp.startup import prepare_startup


async def finished(manager, run):
    async with asyncio.timeout(10):
        while run["status"] in ("queued", "running"):
            await asyncio.sleep(0.01)
            run = manager.get_run(run["run_id"])
    return run


async def opened(manager):
    return (await manager.create_session("fake"))["session_id"]


def test_relocates_startup_preserving_plugin_order_and_paths(tmp_path):
    bin_dir = tmp_path / "install space/bin"
    bin_dir.mkdir(parents=True)
    source = bin_dir / "startup.txt"
    original = """# retained in original
ROOT_PATH = ../
DATA_PATH = ROOT_PATH/data/
FILE_UPDATE_PATH = DATA_PATH/
OUTPUT_PATH = ../output/
LOG_FILE = OUTPUT_PATH/GmatLog.txt
GMAT_FUNCTION_PATH = ROOT_PATH/first
GMAT_FUNCTION_PATH = ROOT_PATH/second
PLUGIN = ../plugins/libB
PLUGIN = ../plugins/libA
"""
    source.write_text(original)
    startup, plugins = prepare_startup(source, bin_dir, tmp_path / "workspace")
    relocated = startup.read_text()
    assert source.read_text() == original
    assert plugins == [(bin_dir.parent / f"plugins/lib{name}").as_posix() for name in ("B", "A")]
    assert relocated.index("/first/") < relocated.index("/second/")
    assert f"DATA_PATH = {bin_dir.parent.as_posix()}/data/" in relocated
    assert f"LOG_FILE = {tmp_path.as_posix()}/workspace/logs/engine.log" in relocated
    assert "WRITE_PERSONALIZATION_FILE = OFF" in relocated


def test_startup_cycles_fail(tmp_path):
    source = tmp_path / "bad.txt"
    source.write_text("ONE_PATH = TWO_PATH/\nTWO_PATH = ONE_PATH/\n")
    with pytest.raises(GmatError, match="Cyclic"):
        prepare_startup(source, tmp_path, tmp_path / "work")


def test_startup_preserves_hash_in_paths_and_rejects_invalid_assignments(tmp_path):
    source = tmp_path / "startup.txt"
    source.write_text("DATA_PATH = ../data#1\n")
    result, _ = prepare_startup(source, tmp_path, tmp_path / "work")
    assert "data#1" in result.read_text()
    source.write_text("not an assignment\n")
    with pytest.raises(GmatError, match="Invalid startup assignment"):
        prepare_startup(source, tmp_path, tmp_path / "work2")


def test_config_resolves_paths_without_resolving_python_symlink(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        '[profiles.test]\nvariant="qt"\nroot="relative"\npython_executable="python3"\n'
    )
    parsed = load_config(config)
    assert parsed.profiles["test"].root == tmp_path / "relative"
    assert parsed.profiles["test"].python_executable == "python3"


async def test_isolation_revision_runtime_and_invalid_fields(manager):
    first, second = await asyncio.gather(opened(manager), opened(manager))
    await manager.call(first, "create_object", mutates=True, type="Spacecraft", name="Sat")
    assert (await manager.call(second, "list_objects"))["objects"] == []
    assert "native import/setup stdout" in manager.get_logs(first)["content"]
    with pytest.raises(GmatError, match="Unknown field"):
        await manager.call(first, "set_field", mutates=True, name="Sat", field="Missing", value=3)
    with pytest.raises(GmatError, match="Read-only"):
        await manager.call(
            first, "set_field", mutates=True, name="Sat", field="Locked", value="bad"
        )
    before = await manager.call(first, "get_object", name="Sat", fields=["X"])
    run = await finished(manager, await manager.run_mission(first))
    assert run["status"] == "succeeded"
    assert run["native_status"] == 1
    directory = Path(run["directory"])
    provenance = json.loads((directory / "provenance.json").read_text())
    assert (
        provenance["startup_sha256"]
        == hashlib.sha256((directory / "effective_startup.txt").read_bytes()).hexdigest()
    )
    after = await manager.call(first, "get_object", name="Sat", fields=["X"], view="runtime")
    assert after["fields"][0]["value"] == before["fields"][0]["value"] + 1
    assert after["run_id"] == run["run_id"]
    await manager.call(first, "set_field", mutates=True, name="Sat", field="DryMass", value=200.0)
    with pytest.raises(GmatError, match="Run the current model"):
        await manager.call(first, "get_object", name="Sat", view="runtime")


async def test_staged_import_roundtrip_and_save(manager, tmp_path):
    sid = await opened(manager)
    await manager.call(sid, "create_object", mutates=True, type="Spacecraft", name="Saved")
    snapshot = await manager.export_script(sid, str(tmp_path / "saved.script"))
    with pytest.raises(GmatError, match="exists"):
        await manager.export_script(sid, str(tmp_path / "saved.script"))
    with pytest.raises(GmatError, match="parsing failed") as error:
        await manager.load_script(sid, text="badparse")
    assert "script syntax error" in error.value.details["diagnostics"]["content"]
    assert (await manager.call(sid, "list_objects"))["objects"] == ["Saved"]
    old_generation = manager.session(sid).generation
    await manager.load_script(sid, text=snapshot["text"])
    assert manager.session(sid).generation == old_generation + 1
    assert (await manager.call(sid, "list_objects"))["objects"] == ["Saved"]


@pytest.mark.parametrize(
    "scenario,timeout,status",
    [("slow", 0.05, "timed_out"), ("crash", None, "failed"), ("failure", None, "failed")],
)
async def test_native_failure_and_timeout(manager, scenario, timeout, status):
    sid = await opened(manager)
    await manager.load_script(sid, text=scenario)
    run = await finished(manager, await manager.run_mission(sid, timeout))
    assert run["status"] == status
    if scenario == "failure":
        assert run["native_status"] == -5
        assert manager.session(sid).state == "ready"
    else:
        assert manager.session(sid).state == "lost"
        assert manager.session(sid).worker.process.returncode is not None
    assert manager.list_artifacts(run["run_id"])["artifacts"]
    await manager.reset_session(sid)
    assert manager.session(sid).state == "ready"


@pytest.mark.parametrize("immediate", [True, False])
async def test_cancel_and_busy_session(manager, immediate):
    sid = await opened(manager)
    await manager.load_script(sid, text="slow")
    run = await manager.run_mission(sid)
    if not immediate:
        await asyncio.sleep(0.03)
    with pytest.raises(GmatError) as busy:
        await manager.call(sid, "list_objects")
    assert busy.value.code == "SESSION_BUSY"
    assert manager.session(sid).describe()["active_run"] == run["run_id"]
    manager.get_logs(sid)
    cancelled = await manager.cancel_run(run["run_id"])
    assert cancelled["status"] == "cancelled"
    assert not manager.session(sid).lock.locked()
    # Reload can recover a lost session, and cancellation never replays a run.
    await manager.load_script(sid, text="normal")
    assert len(manager.runs) == 1


async def test_artifact_bounds_and_symlinks(manager, tmp_path):
    sid = await opened(manager)
    await manager.load_script(sid, text="normal")
    run = await finished(manager, await manager.run_mission(sid))
    artifacts = manager.list_artifacts(run["run_id"])["artifacts"]
    report = next(item for item in artifacts if item["path"] == "output/report.txt")
    read = manager.read_artifact(run["run_id"], report["id"], limit=3)
    assert read["content"] == "sta" and read["has_more"] and read["next_offset"] == 3
    with pytest.raises(GmatError, match="limit"):
        manager.read_artifact(run["run_id"], report["id"], limit=1000000)
    with pytest.raises(GmatError, match="does not exist"):
        manager.read_artifact(run["run_id"], "../../outside")
    if sys.platform != "win32":
        outside = tmp_path / "outside.txt"
        outside.write_text("private")
        (Path(run["directory"]) / "link.txt").symlink_to(outside)
        assert not any(
            a["path"] == "link.txt" for a in manager.list_artifacts(run["run_id"])["artifacts"]
        )


async def test_long_summaries_remain_in_artifacts(manager):
    sid = await opened(manager)
    await manager.load_script(sid, text="longsummary")
    run = await finished(manager, await manager.run_mission(sid))
    assert run["status"] == "succeeded"
    assert len(run["summary"]) == 65536
    assert run["summary_truncated"]
    summary = next(
        a for a in manager.list_artifacts(run["run_id"])["artifacts"] if a["path"] == "summary.txt"
    )
    assert summary["size"] == len("long summary\n" * 10000)
    chunk = manager.read_artifact(run["run_id"], summary["id"], offset=65536)
    assert chunk["content"]


async def test_limits_missing_api_interpreter_and_shutdown(fake_config):
    fake_config.server.max_sessions = 1
    manager = Manager(fake_config)
    try:
        sid = await opened(manager)
        with pytest.raises(GmatError) as full:
            await opened(manager)
        assert full.value.code == "SESSION_LIMIT"
        worker = manager.session(sid).worker
        await manager.shutdown()
        assert worker.process.returncode is not None
    finally:
        await manager.shutdown()
    manager = Manager(fake_config)
    profile = fake_config.profiles["fake"]
    profile.python_executable = str(fake_config.server.data_dir / "nonexistent-python")
    with pytest.raises(GmatError, match="Cannot launch"):
        await opened(manager)
    profile.python_executable = sys.executable
    package = next((profile.bin_dir / "gmatpy").glob("_py*"))
    package.rename(package.with_name("wrong_abi"))
    with pytest.raises(GmatError, match="Select a matching"):
        await opened(manager)


async def test_reports_unavailable_plugins_and_rejects_nonfinite_values(fake_config):
    startup = fake_config.profiles["fake"].startup
    with startup.open("a") as stream:
        stream.write("PLUGIN = ../plugins/missing\n")
    manager = Manager(fake_config)
    try:
        result = await manager.create_session("fake")
        assert result["engine"]["plugins"][0]["status"] == "missing"
        sid = result["session_id"]
        await manager.call(sid, "create_object", mutates=True, type="Spacecraft", name="Sat")
        with pytest.raises(GmatError, match="finite"):
            await manager.call(
                sid, "set_field", mutates=True, name="Sat", field="DryMass", value=float("nan")
            )
    finally:
        await manager.shutdown()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux R2026a versioned library packaging")
def test_versioned_loader_aliases_stay_outside_installation(tmp_path):
    bin_dir = tmp_path / "install/bin"
    plugin_dir = tmp_path / "install/plugins"
    bin_dir.mkdir(parents=True)
    plugin_dir.mkdir()
    binary = plugin_dir / "libTest.so.R2026a"
    binary.write_bytes(b"test library")
    loader, aliases = prepare_loader(bin_dir, tmp_path / "session")
    assert (loader / "libTest.so").resolve() == binary
    assert not (plugin_dir / "libTest.so").exists()
    startup = bin_dir / "startup.txt"
    startup.write_text("PLUGIN = ../plugins/libTest\nPLUGIN = ../plugins/libOther\n")
    effective, plugins = prepare_startup(startup, bin_dir, tmp_path / "session", aliases)
    assert plugins == [str(loader / "libTest"), str(plugin_dir / "libOther")]
    assert effective.is_file()
