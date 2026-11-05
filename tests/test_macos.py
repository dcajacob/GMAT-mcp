"""Mach-O and app layouts are tested portably; actual dyld is tested on Macs."""

from __future__ import annotations

import shutil
import struct
import sys

import pytest

from gmat_mcp.config import Config, Profile, Settings, load_config
from gmat_mcp.core import Manager
from gmat_mcp.discovery import discover, write_config
from gmat_mcp.installation import bin_directory, installation_root, library_directories
from gmat_mcp.native_platform import (
    binary_architecture,
    binary_architectures,
    binding_architectures,
)
from gmat_mcp.process import worker_environment


def thin(cpu, endian="<", bits=64):
    magic = 0xFEEDFACF if bits == 64 else 0xFEEDFACE
    return struct.pack(endian + "IIIIIIII", magic, cpu, 0, 6, 0, 0, 0, 0)


def fat(arches, endian=">", wide=False):
    header = struct.pack(endian + "II", 0xCAFEBABF if wide else 0xCAFEBABE, len(arches))
    records, payload = b"", b""
    for index, cpu in enumerate(arches):
        records += struct.pack(
            endian + ("IIQQII" if wide else "IIIII"),
            cpu,
            0,
            128 + index * 32,
            32,
            0,
            *([0] if wide else []),
        )
        payload += thin(cpu)
    return (header + records).ljust(128, b"\0") + payload


@pytest.mark.parametrize("endian", ["<", ">"])
@pytest.mark.parametrize(
    "bits,cpu,expected", [(32, 7, "x86"), (64, 0x1000007, "x86_64"), (64, 0x100000C, "arm64")]
)
def test_thin_macho(tmp_path, endian, bits, cpu, expected):
    path = tmp_path / "module.so"
    path.write_bytes(thin(cpu, endian, bits))
    assert binary_architectures(path) == {expected}
    assert binary_architecture(path) == expected


@pytest.mark.parametrize("endian", ["<", ">"])
@pytest.mark.parametrize("wide", [False, True])
def test_universal_macho_and_corrupt_slices(tmp_path, endian, wide):
    path = tmp_path / "module.so"
    contents = fat([0x1000007, 0x100000C], endian, wide)
    path.write_bytes(contents)
    assert binary_architecture(path) == "universal2"
    assert binary_architectures(path) == {"x86_64", "arm64"}
    path.write_bytes(contents[:-1])
    assert binary_architectures(path) is None
    path.write_bytes(contents[:128] + thin(0x100000C) + thin(0x1000007))
    assert binary_architectures(path) is None


def test_binding_slice_intersection_and_discovery(fake_config, monkeypatch):
    from gmat_mcp import discovery

    binary = fake_config.profiles["fake"].bin_dir
    bindings = binary / f"gmatpy/_py{sys.version_info.major}{sys.version_info.minor}"
    (bindings / "_gmat_py.so").write_bytes(fat([0x1000007, 0x100000C]))
    (bindings / "_station_py.so").write_bytes(thin(0x1000007))
    assert binding_architectures(bindings) == {"x86_64"}
    monkeypatch.setattr(
        discovery,
        "probe_interpreter",
        lambda exe: {
            "executable": exe,
            "version": list(sys.version_info[:3]),
            "architecture": "arm64",
        },
    )
    assert not discover([binary.parent], [sys.executable])["profiles"][0]["ready"]
    (bindings / "_station_py.so").write_bytes(thin(0x100000C))
    assert discover([binary.parent], [sys.executable])["profiles"][0]["ready"]
    (bindings / "_navigation_py.so").write_bytes(thin(0x1000007))
    assert binding_architectures(bindings) == set()
    assert not discover([binary.parent], [sys.executable])["profiles"][0]["ready"]


async def test_app_bundle_profile_discovery_and_worker(fake_config, tmp_path):
    app = tmp_path / "GMAT Qt.app"
    binary = app / "Contents/MacOS"
    shutil.copytree(fake_config.profiles["fake"].bin_dir, binary)
    (binary / "gmat_startup_file.txt").unlink()
    startup = tmp_path / "gmat_startup_qt.txt"
    startup.write_text("ROOT_PATH = ../Resources/\nOUTPUT_PATH = ../../../output/\n")
    (app / "Contents/Resources/data").mkdir(parents=True)
    (app / "Contents/Frameworks").mkdir()
    profile = Profile(variant="qt", root=app)
    assert profile.bin_dir == binary and profile.startup == startup
    assert profile.data_dir == app / "Contents/Resources/data"
    report = discover([app], [sys.executable])
    assert len(report["profiles"]) == 1
    assert report["profiles"][0]["root"] == str(app)
    assert report["profiles"][0]["startup_file"] == str(startup)
    # Fake engine has no native extension; make its discovery fixture explicit.
    bindings = binary / f"gmatpy/_py{sys.version_info.major}{sys.version_info.minor}"
    (bindings / "_gmat_py.pyd").touch()
    report = discover([app], [sys.executable])
    config_path = tmp_path / "profiles.toml"
    write_config(report, config_path)
    assert load_config(config_path).profiles["gmat-qt"].bin_dir == binary
    manager = Manager(
        Config(server=Settings(data_dir=tmp_path / "artifacts"), profiles={"qt": profile})
    )
    try:
        opened = await manager.create_session("qt")
        sid = opened["session_id"]
        await manager.call(sid, "create_object", mutates=True, type="Spacecraft", name="BundleSat")
        assert "BundleSat" in (await manager.export_script(sid))["text"]
    finally:
        await manager.shutdown()
    assert startup.read_text().startswith("ROOT_PATH = ../Resources/")


def test_nasa_app_aliases_and_selected_dyld_directories(tmp_path, monkeypatch):
    from gmat_mcp import process

    root = tmp_path / "NASA GMAT R2026a"
    binary = root / "bin"
    binary.mkdir(parents=True)
    (binary / "gmat_startup_file.txt").touch()
    app = binary / "GMAT-R2026a_Beta.app"
    framework = app / "Contents/Frameworks"
    (app / "Contents/MacOS").mkdir(parents=True)
    framework.mkdir()
    target = framework / "gmatpy/_py311/_gmat_py.so"
    target.parent.mkdir(parents=True)
    target.write_bytes(fat([0x1000007, 0x100000C]))
    binding = binary / "gmatpy/_py311/_gmat_py.so"
    binding.parent.mkdir(parents=True)
    binding.symlink_to(target)
    assert installation_root(app) == root
    assert bin_directory(app) == binary
    assert framework in library_directories(binary)
    monkeypatch.setattr(process.sys, "platform", "darwin")
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/some/other/GMAT")
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", "/some/other/dylib")
    inherited = (
        "DYLD_FRAMEWORK_PATH",
        "DYLD_FALLBACK_FRAMEWORK_PATH",
        "DYLD_VERSIONED_LIBRARY_PATH",
        "DYLD_VERSIONED_FRAMEWORK_PATH",
        "DYLD_ROOT_PATH",
        "DYLD_IMAGE_SUFFIX",
    )
    for variable in inherited:
        monkeypatch.setenv(variable, "/some/other/GMAT")
    environment = worker_environment(binary)
    assert str(framework) in environment["DYLD_LIBRARY_PATH"]
    assert "other/GMAT" not in environment["DYLD_LIBRARY_PATH"]
    assert "DYLD_INSERT_LIBRARIES" not in environment
    assert not any(variable in environment for variable in inherited)
