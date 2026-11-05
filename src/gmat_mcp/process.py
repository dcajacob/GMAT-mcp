"""Platform-specific native loader environment and process lifecycle."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

from .errors import GmatError
from .installation import library_directories


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return (
                bool(kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)))
                and code.value == 259
            )
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def prepare_loader(bin_dir: Path, workspace: Path) -> tuple[Path, dict[str, str]]:
    """Restore loader names for Linux R2026a packages with versioned-only libraries.

    All aliases stay inside this worker's workspace. Never repair an installation
    in place, and never select libraries from another installation.
    """
    loader = workspace / "loader"
    loader.mkdir(parents=True, exist_ok=True)
    aliases: dict[str, str] = {}
    if sys.platform != "linux":
        return loader, aliases
    targets: dict[str, Path] = {}
    for directory in (bin_dir, bin_dir.parent / "lib", bin_dir.parent / "plugins"):
        for versioned in sorted(directory.rglob("*.so.R2026a")):
            unversioned = versioned.with_name(versioned.name.removesuffix(".R2026a"))
            if unversioned.exists():
                continue
            name = unversioned.name
            if name in targets and targets[name].resolve() != versioned.resolve():
                raise GmatError("ENGINE_SETUP", f"Ambiguous versioned native library: {name}")
            targets[name] = versioned
            alias = loader / name
            if not alias.exists():
                alias.symlink_to(versioned.resolve())
            aliases[str(unversioned.with_suffix(""))] = str(alias.with_suffix(""))
    return loader, aliases


def worker_environment(bin_dir: Path, loader: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    # Do not inherit another installation's Python imports.
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if sys.platform == "win32":
        # R2026a FileUtil detects Windows through getenv("OS"). MCP stdio
        # clients may deliberately inherit only a small environment without it.
        env["OS"] = "Windows_NT"
    paths = [str(p) for p in library_directories(bin_dir)]
    if loader is not None:
        paths.insert(0, str(loader))
    key = (
        "PATH"
        if sys.platform == "win32"
        else ("DYLD_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH")
    )
    if sys.platform == "darwin":
        # Only the selected installation supplies GMAT dyld overrides.
        env.pop("DYLD_INSERT_LIBRARIES", None)
        env.pop("DYLD_FRAMEWORK_PATH", None)
        env[key] = os.pathsep.join(paths)
        env["DYLD_FALLBACK_LIBRARY_PATH"] = os.pathsep.join(paths)
    else:
        env[key] = os.pathsep.join([*paths, env.get(key, "")])
    return env


def spawn_options() -> dict:
    if sys.platform == "win32":
        import subprocess

        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


async def terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        await process.wait()
        return
    try:
        await asyncio.wait_for(process.wait(), 3)
    except TimeoutError:
        try:
            if sys.platform == "win32":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
