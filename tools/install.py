"""Install a verified release bundle into a new virtual environment."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import venv
from pathlib import Path


def verify(bundle: Path):
    entries = json.loads((bundle / "SHA256.json").read_text(encoding="utf-8"))
    for name, expected in entries.items():
        path = (bundle / name).resolve()
        if not path.is_relative_to(bundle.resolve()) or not path.is_file():
            raise ValueError(f"Missing or unsafe bundle entry: {name}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Bundle checksum mismatch: {name}")
    wheels = list(bundle.glob("gmat_mcp-*-py3-none-any.whl"))
    if len(wheels) != 1 or wheels[0].name not in entries or "requirements.lock" not in entries:
        raise ValueError("Bundle must contain exactly one checksummed server wheel and lock")
    return wheels[0]


def install(bundle: Path, destination: Path):
    if sys.version_info < (3, 11):
        raise ValueError("Use CPython 3.11 or newer")
    wheel = verify(bundle)
    destination = destination.expanduser().absolute()
    # Refuse to reuse another environment; installation cannot replace saved work.
    destination.mkdir(parents=True, exist_ok=False)
    venv.EnvBuilder(with_pip=True).create(destination)
    scripts = destination / ("Scripts" if sys.platform == "win32" else "bin")
    python = scripts / ("python.exe" if sys.platform == "win32" else "python")
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--require-hashes",
            "-r",
            str(bundle / "requirements.lock"),
        ],
        check=True,
    )
    subprocess.run([str(python), "-m", "pip", "install", "--no-deps", str(wheel)], check=True)
    command = scripts / ("gmat-mcp.exe" if sys.platform == "win32" else "gmat-mcp")
    print(
        json.dumps(
            {
                "command": str(command),
                "next": "Run discover, init, then doctor with your GMAT roots",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    install(Path(__file__).resolve().parent, args.destination)
