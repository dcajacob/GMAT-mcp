"""Prepare disposable NASA/Qt installations on a native GitHub macOS runner.

Downloads stay outside the checkout. No installed application or security policy is
modified. The receipt identifies downloaded bytes and the exact Qt source revision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

NASA_URL = (
    "https://downloads.sourceforge.net/project/gmat/GMAT/GMAT-R2026a/gmat-mac-x64-R2026a-signed.dmg"
)
NASA_SHA256 = "8a81d22ce2d39504eadc875d71494321adb49f60da92d266fdbe660de0f771d2"
NASA_MIRRORS = (
    NASA_URL.replace("downloads.sourceforge.net", "gigenet.dl.sourceforge.net"),
    NASA_URL.replace("downloads.sourceforge.net", "master.dl.sourceforge.net"),
)
ROOT = Path(__file__).resolve().parents[1]


def run(*args, **kwargs):
    print("+", " ".join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def output(*args):
    return run(*args, capture_output=True, text=True).stdout.strip()


def file_digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def download(url, destination, *, expected_sha256=None, fallbacks=()):
    if destination.is_file() and expected_sha256 and file_digest(destination) == expected_sha256:
        print(f"Using verified download cache: {destination.name}", flush=True)
        return {"url": url, "sha256": expected_sha256, "cached": True}
    partial = destination.with_name(destination.name + ".part")
    errors = []
    for candidate in (url, *fallbacks):
        try:
            options = [
                "--fail",
                "--location",
                "--connect-timeout",
                "20",
                "--max-time",
                "600",
                "--retry",
                "1",
                "--retry-all-errors",
                "--retry-delay",
                "1",
                "--silent",
                "--show-error",
                "--write-out",
                "%{speed_download}",
            ]
            if fallbacks:
                # CI should change mirrors instead of spending half an hour on
                # a throttled connection. Smaller JPL downloads keep normal retries.
                options += ["--speed-limit", "1048576", "--speed-time", "30"]
            result = run(
                "curl", *options, "--output", partial, candidate, capture_output=True, text=True
            )
            digest = file_digest(partial)
            if expected_sha256 and digest != expected_sha256:
                raise ValueError(f"SHA-256 mismatch: expected {expected_sha256}, received {digest}")
            partial.replace(destination)
            print(
                f"Downloaded {destination.name}: {result.stdout.strip()} bytes/second", flush=True
            )
            return {"url": url, "mirror": candidate, "sha256": digest, "cached": False}
        except (subprocess.CalledProcessError, ValueError) as error:
            diagnostic = (
                error.stderr if isinstance(error, subprocess.CalledProcessError) else str(error)
            )
            errors.append(f"{candidate}: {diagnostic}")
            print(f"Download attempt failed: {errors[-1]}", flush=True)
        finally:
            partial.unlink(missing_ok=True)
    raise RuntimeError("All download attempts failed:\n" + "\n".join(errors))


def nasa_install(work):
    image = work / "nasa.dmg"
    receipt = download(NASA_URL, image, expected_sha256=NASA_SHA256, fallbacks=NASA_MIRRORS)
    if os.environ.get("GITHUB_ENV"):
        with open(os.environ["GITHUB_ENV"], "a") as file:
            file.write("GMAT_NASA_DOWNLOADED=1\n")
    mount = work / "nasa-mount"
    mount.mkdir()
    run("hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", mount, image)
    try:
        candidates = [
            path.parent.parent
            for path in mount.rglob("gmat_startup_file.txt")
            if path.parent.name == "bin"
            and (path.parent / "gmatpy").is_dir()
            and (path.parent.parent / "data").is_dir()
        ]
        if len(candidates) != 1:
            raise RuntimeError(
                f"Expected one complete NASA installation in DMG, found {candidates}"
            )
        destination = work / "NASA R2026a"
        shutil.copytree(candidates[0], destination, symlinks=True)
    finally:
        run("hdiutil", "detach", mount)
    return destination, receipt


def qt_install(checkout, worker, work):
    architecture = platform.machine()
    if architecture not in ("arm64", "x86_64"):
        raise RuntimeError(f"Unsupported native runner architecture: {architecture}")
    toolkit = "MacM1_OSX_clang_64bit" if architecture == "arm64" else "MacIntel_OSX_AppleC_64bit"
    archive = work / "cspice.tar.Z"
    receipt = download(
        f"https://naif.jpl.nasa.gov/pub/naif/toolkit/C/{toolkit}/packages/cspice.tar.Z", archive
    )
    dependencies = work / "dependencies"
    dependencies.mkdir()
    run("tar", "-xf", archive, "-C", dependencies)
    run(sys.executable, ROOT / "integrations/install_qt_bridge.py", checkout, "--apply")
    version = output(
        worker, "-c", "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    )
    python_root = output(worker, "-c", "import sys;print(sys.base_prefix)")
    prefixes = [
        output("brew", "--prefix", package) for package in ("qt", "open-scene-graph", "xerces-c")
    ]
    build = work / "qt-build"
    options = {
        "CMAKE_BUILD_TYPE": "Release",
        "CMAKE_OSX_DEPLOYMENT_TARGET": "15.0",
        "CMAKE_OSX_ARCHITECTURES": architecture,
        "CMAKE_PREFIX_PATH": ";".join(prefixes),
        "GMAT_INCLUDE_GUI": "OFF",
        "GMAT_INCLUDE_QT_GUI": "ON",
        "GMAT_QT_BUILD_TESTS": "ON",
        "GMAT_INCLUDE_API": "ON",
        "API_GENERATE_PYTHON": "ON",
        "API_GENERATE_JAVA": "OFF",
        "GMAT_PYTHON3_VERSIONS": version,
        f"GMAT_PYTHON{version.replace('.', '')}_ROOT_DIR": python_root,
        "Python3_EXECUTABLE": str(worker),
        "SWIG_EXECUTABLE": shutil.which("swig"),
        "CSPICE_DIR": str(dependencies / "cspice"),
        "GMAT_QT_EXTERNAL_PLUGINS": "",
    }
    for plugin in (
        "MATLABINTERFACE",
        "FMINCONOPTIMIZER",
        "PYTHONINTERFACE",
        "EXTERNALFORCEMODEL",
        "OVTOOFI",
        "CINTERFACE",
    ):
        options[f"PLUGIN_{plugin}"] = "OFF"
    run("cmake", "-S", checkout, "-B", build, *[f"-D{k}={v}" for k, v in options.items()])
    suffix = version.replace(".", "")
    run(
        "cmake",
        "--build",
        build,
        "--parallel",
        "3",
        "--target",
        f"gmat_py{suffix}",
        f"station_py{suffix}",
        f"navigation_py{suffix}",
        "GmatQt",
        "GmatQtMcpBridgeTests",
    )
    receipt["source_commit"] = output("git", "-C", checkout, "rev-parse", "HEAD")
    receipt["cmake_options"] = options
    return checkout / "application", build, receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qt-source", type=Path, required=True)
    parser.add_argument("--worker-python", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("test-results/macos"))
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("Run on a native macOS machine")
    args.work.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)
    nasa, nasa_receipt = nasa_install(args.work)
    (args.output / "nasa-download.json").write_text(json.dumps(nasa_receipt, indent=2) + "\n")
    qt, build, qt_receipt = qt_install(args.qt_source.resolve(), args.worker_python, args.work)
    receipt = {
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "worker": output(args.worker_python, "--version"),
        "homebrew": output("brew", "list", "--versions"),
        "nasa": nasa_receipt,
        "qt": qt_receipt,
    }
    (args.output / "preparation.json").write_text(json.dumps(receipt, indent=2) + "\n")
    config = args.work / "profiles.toml"
    config.write_text(
        "[server]\nmax_sessions = 4\nrun_timeout = 1800\noperation_timeout = 60\n"
        f"data_dir = {json.dumps(str(args.work / 'mcp-data'))}\n"
        + "".join(
            f"\n[profiles.{name}]\nvariant = {json.dumps(variant)}\n"
            f"root = {json.dumps(str(root))}\n"
            f"python_executable = {json.dumps(str(args.worker_python))}\n"
            for name, variant, root in (("nasa-r2026a", "nasa", nasa), ("gmat-qt", "qt", qt))
        )
    )
    # Only non-secret paths are passed between workflow steps.
    if os.environ.get("GITHUB_ENV"):
        with open(os.environ["GITHUB_ENV"], "a") as file:
            file.write(f"GMAT_MCP_TEST_CONFIG={config}\nGMAT_QT_TEST_BUILD={build}\n")
    print(f"Configuration: {config}")


if __name__ == "__main__":
    main()
