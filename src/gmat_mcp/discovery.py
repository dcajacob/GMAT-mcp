"""Bounded local discovery. Interpreter probes never import native GMAT code."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections import deque
from pathlib import Path

from .config import Config, Profile, Settings
from .errors import GmatError
from .installation import bin_directory, installation_root, startup_candidates
from .native_platform import architecture_label, binding_architectures


def interpreter_candidates(explicit=()):
    candidates = [*explicit, sys.executable]
    for minor in range(11, 16):
        candidate = shutil.which(f"python3.{minor}")
        if candidate:
            candidates.append(candidate)
    if sys.platform == "win32":
        launcher = shutil.which("py")
        if launcher:
            try:
                result = subprocess.run(
                    [launcher, "--list-paths"], capture_output=True, text=True, timeout=5
                )
                for line in result.stdout.splitlines():
                    match = re.search(r"([A-Za-z]:\\.*?python(?:3)?\.exe)\s*$", line, re.I)
                    if match:
                        candidates.append(match[1])
            except (OSError, subprocess.TimeoutExpired):
                pass
        for parent in (
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Python",
            Path(os.environ.get("ProgramFiles", "C:/Program Files")),
        ):
            candidates.extend(str(p) for p in parent.glob("Python*/python.exe"))
    if sys.platform == "darwin":
        for parent in (Path("/opt/homebrew/opt"), Path("/usr/local/opt")):
            candidates.extend(
                str(p)
                for p in parent.glob("python@3.*/bin/python3.*")
                if p.name.removeprefix("python3.").isdigit()
            )
        candidates.extend(
            str(p)
            for p in Path("/Library/Frameworks/Python.framework/Versions").glob("3.*/bin/python3.*")
            if p.name.removeprefix("python3.").isdigit()
        )
    return list(
        dict.fromkeys(
            os.path.abspath(os.path.expandvars(os.path.expanduser(p)))
            if "/" in p or "\\" in p
            else p
            for p in candidates
        )
    )


def probe_interpreter(executable):
    if "windowsapps" in executable.lower():
        return {
            "executable": executable,
            "error": "Windows Store alias; select an installed CPython executable",
        }
    code = "import sys,json,struct,platform;print(json.dumps({'version':list(sys.version_info[:3]),'implementation':sys.implementation.name,'bits':struct.calcsize('P')*8,'machine':platform.machine()}))"
    try:
        result = subprocess.run(
            [executable, "-I", "-c", code], capture_output=True, text=True, timeout=5
        )
        data = json.loads(result.stdout.strip())
        if (
            result.returncode
            or data["implementation"] != "cpython"
            or data["version"][:2] < [3, 11]
        ):
            raise ValueError("Requires CPython 3.11 or newer")
        data["executable"] = executable
        data["architecture"] = (
            "x86"
            if data["bits"] == 32
            else {"amd64": "x86_64", "aarch64": "arm64"}.get(
                data["machine"].lower(), data["machine"].lower()
            )
        )
        return data
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        return {"executable": executable, "error": str(exc)}


def default_roots():
    roots = [Path.home() / "Downloads/GMAT", Path.home() / "GIT/GMAT-Qt"]
    roots += [Path(os.environ[k]) for k in ("GMAT_ROOT", "GMAT_QT_ROOT") if os.environ.get(k)]
    if sys.platform == "win32":
        roots.append(Path("C:/GMAT"))
        for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            if os.environ.get(key):
                roots.extend(Path(os.environ[key]).glob("GMAT*"))
    if sys.platform == "darwin":
        for parent in (
            Path("/Applications"),
            Path.home() / "Applications",
            Path.home() / "Downloads",
        ):
            roots.extend(parent.glob("*GMAT*"))
            roots.extend(parent.glob("*gmat*"))
    return roots


def installation_roots(roots, max_depth=5, max_directories=5000):
    pending = deque((Path(root).expanduser().resolve(), 0) for root in roots)
    visited = set()
    found = set()
    while pending and len(visited) < max_directories:
        directory, depth = pending.popleft()
        directory = installation_root(directory)
        if directory in visited or not directory.is_dir():
            continue
        visited.add(directory)
        if directory.name == "bin" and (directory / "gmat_startup_file.txt").exists():
            directory = directory.parent
        if any(path.is_file() for path in startup_candidates(directory)):
            found.add(directory)
            continue
        if depth >= max_depth:
            continue
        try:
            pending.extend(
                (p, depth + 1)
                for p in sorted(directory.iterdir())
                if p.is_dir()
                and not p.is_symlink()
                and not p.name.startswith(".")
                and p.name not in ("node_modules", "data", "docs", "plugins")
            )
        except OSError:
            continue
    return sorted(found), bool(pending)


def discover(roots=(), pythons=()):
    interpreters = [probe_interpreter(p) for p in interpreter_candidates(pythons)]
    installations, limited = installation_roots(roots or default_roots())
    profiles = []
    counts = {}
    for root in installations:
        variant = (
            "qt"
            if any(
                p.name == "gmat_startup_qt.txt" and p.is_file() for p in startup_candidates(root)
            )
            else "nasa"
        )
        base = (
            "gmat-qt"
            if variant == "qt"
            else "nasa-r2026a"
            if "2026" in str(root).lower()
            else "nasa-gmat"
        )
        counts[base] = counts.get(base, 0) + 1
        identifier = base if counts[base] == 1 else f"{base}-{counts[base]}"
        bindings = []
        compatible = []
        for path in sorted((bin_directory(root) / "gmatpy").glob("_py*")):
            match = re.fullmatch("_py(3)([0-9]+)", path.name)
            if not match:
                continue
            version = [int(match[1]), int(match[2])]
            arches = binding_architectures(path)
            arch = architecture_label(arches)
            present = any(
                p.is_file()
                for pattern in ("_gmat_py*.pyd", "_gmat_py*.so", "_gmat_py*.dylib")
                for p in path.glob(pattern)
            )
            bindings.append(
                {
                    "python": version,
                    "architecture": arch,
                    "architectures": sorted(arches) if arches is not None else None,
                    "directory": str(path),
                    "present": present,
                }
            )
            if not present:
                continue
            compatible += [
                p
                for p in interpreters
                if not p.get("error")
                and p["version"][:2] == version
                and (arches is None or p["architecture"] in arches)
            ]
        selected = next((p for p in compatible if p["executable"] in pythons), None) or next(
            iter(compatible), None
        )
        startup = Profile(variant=variant, root=root).startup
        profiles.append(
            {
                "id": identifier,
                "variant": variant,
                "root": str(root),
                "startup_file": str(startup),
                "python_executable": selected["executable"] if selected else None,
                "bindings": bindings,
                "ready": selected is not None,
                "setup": None
                if selected
                else "Install matching 64-bit CPython 3.11+ or build API bindings with -DGMAT_INCLUDE_API=ON -DAPI_GENERATE_PYTHON=ON",
            }
        )
    return {"profiles": profiles, "interpreters": interpreters, "search_limited": limited}


def write_config(report, path: Path, overwrite=False):
    profiles = {
        p["id"]: Profile(
            variant=p["variant"],
            root=Path(p["root"]),
            startup_file=Path(p["startup_file"]),
            python_executable=p["python_executable"],
        )
        for p in report["profiles"]
        if p["ready"]
    }
    if not profiles:
        raise GmatError(
            "SETUP_REQUIRED",
            "No installation has compatible bindings and an interpreter; inspect discover output",
        )
    config = Config(server=Settings(), profiles=profiles)
    lines = [
        "# Generated by gmat-mcp init; installations are not modified.",
        "[server]",
        "max_sessions = 4",
        "run_timeout = 1800",
        "",
    ]
    for name, profile in config.profiles.items():
        lines.append(f"[profiles.{json.dumps(name)}]")
        for key in ("variant", "root", "startup_file", "python_executable"):
            lines.append(f"{key} = {json.dumps(str(getattr(profile, key)), ensure_ascii=False)}")
        lines.append("")
    path = path.expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w" if overwrite else "x", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(lines))
    except OSError as exc:
        raise GmatError(
            "CONFIG_EXISTS",
            f"Cannot write {path}: {exc}; choose another output or explicitly use --overwrite",
        ) from exc
    return {
        "path": str(path),
        "profiles": list(profiles),
        "skipped": [p["id"] for p in report["profiles"] if not p["ready"]],
    }
