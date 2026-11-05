from __future__ import annotations

import json
import struct
import sys

import pytest

from gmat_mcp.config import load_config
from gmat_mcp.discovery import discover, installation_roots, probe_interpreter, write_config
from gmat_mcp.errors import GmatError
from gmat_mcp.native_platform import binary_architecture, binding_architecture


@pytest.fixture(autouse=True)
def discovery_fixture_binding(fake_config):
    # Discovery checks for native bindings; the fake worker itself uses Python.
    package = fake_config.profiles["fake"].bin_dir / "gmatpy"
    (package / f"_py{sys.version_info.major}{sys.version_info.minor}" / "_gmat_py.pyd").touch()


def test_discovery_generation_and_no_overwrite(fake_config, tmp_path):
    root = fake_config.profiles["fake"].root
    report = discover([root], [sys.executable])
    assert len(report["profiles"]) == 1 and report["profiles"][0]["ready"]
    output = tmp_path / "profiles with spaces.toml"
    write_config(report, output)
    parsed = load_config(output)
    assert next(iter(parsed.profiles.values())).root == root
    old = output.read_bytes()
    with pytest.raises(GmatError):
        write_config(report, output)
    assert output.read_bytes() == old
    write_config(report, output, True)
    assert next(iter(parsed.profiles.values())).python_executable == sys.executable


def test_discovery_missing_bindings_and_bounded_scan(tmp_path):
    root = tmp_path / "GMAT R2026a"
    (root / "bin").mkdir(parents=True)
    (root / "bin/gmat_startup_file.txt").write_text("")
    found = discover([root], [sys.executable])
    assert not found["profiles"][0]["ready"]
    with pytest.raises(GmatError):
        write_config(found, tmp_path / "no.toml")
    parent = tmp_path / "search"
    (parent / "a" / "b" / "bin").mkdir(parents=True)
    (parent / "a" / "b" / "bin/gmat_startup_file.txt").write_text("")
    assert installation_roots([parent], max_depth=0)[0] == []
    assert installation_roots([parent], max_depth=3)[0] == [parent / "a" / "b"]


def test_architecture_pe_elf_and_interpreter(tmp_path):
    pe = bytearray(128)
    pe[:2] = b"MZ"
    struct.pack_into("<I", pe, 60, 64)
    pe[64:68] = b"PE\0\0"
    struct.pack_into("<H", pe, 68, 0x8664)
    path = tmp_path / "binding.pyd"
    path.write_bytes(pe)
    assert binary_architecture(path) == "x86_64" and binding_architecture(tmp_path) == "x86_64"
    elf = bytearray(64)
    elf[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", elf, 18, 183)
    path.write_bytes(elf)
    assert binary_architecture(path) == "arm64"
    path.write_bytes(b"not a binary")
    assert binary_architecture(path) is None
    info = probe_interpreter(sys.executable)
    assert info["version"][:2] == list(sys.version_info[:2]) and "error" not in info
    assert probe_interpreter(str(tmp_path / "missing-python"))["error"]


def test_worker_restores_windows_path_detection_for_minimal_client(monkeypatch, tmp_path):
    from gmat_mcp import process

    monkeypatch.delenv("OS", raising=False)
    monkeypatch.setattr(process.sys, "platform", "win32")
    assert process.worker_environment(tmp_path)["OS"] == "Windows_NT"


def test_cli_discovery_without_existing_config(fake_config, tmp_path):
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gmat_mcp",
            "--config",
            str(tmp_path / "absent.toml"),
            "discover",
            "--root",
            str(fake_config.profiles["fake"].root),
            "--python",
            sys.executable,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["profiles"][0]["ready"]


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows Job Object contract")
async def test_windows_job_kills_descendants(fake_config, tmp_path):
    import asyncio

    from gmat_mcp.process import process_alive
    from gmat_mcp.windows_job import WindowsJob

    # No native engine: a gated private process creates its child only after it
    # belongs to the job. Closing the job must kill both, including on parent exit.
    code = "import sys,subprocess,time;sys.stdin.readline();p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);print(p.pid,flush=True);time.sleep(120)"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        code,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    job = WindowsJob(process.pid)
    try:
        process.stdin.write(b"go\n")
        await process.stdin.drain()
        child = int(await asyncio.wait_for(process.stdout.readline(), 10))
        assert process_alive(child)
        job.close()
        await asyncio.wait_for(process.wait(), 10)
        async with asyncio.timeout(10):
            while process_alive(child):
                await asyncio.sleep(0.05)
    finally:
        job.close()
        if process.returncode is None:
            process.kill()
            await process.wait()
