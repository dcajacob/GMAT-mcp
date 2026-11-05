"""Build an inspectable wheel/source/locked-dependency installation bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(output: Path, uv: str):
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run([uv, "lock", "--check"], cwd=ROOT, check=True)
    with tempfile.TemporaryDirectory(prefix="gmat-mcp-release-") as temporary:
        stage = Path(temporary) / f"gmat-mcp-{version}"
        stage.mkdir()
        subprocess.run([uv, "build", "--out-dir", str(stage)], cwd=ROOT, check=True)
        subprocess.run(
            [
                uv,
                "export",
                "--frozen",
                "--no-dev",
                "--no-emit-project",
                "--format",
                "requirements-txt",
                "--output-file",
                str(stage / "requirements.lock"),
            ],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        for name in ("README.md", "VALIDATION.md", "LICENSE", "uv.lock"):
            shutil.copyfile(ROOT / name, stage / name)
        for name in ("examples", "integrations"):
            shutil.copytree(ROOT / name, stage / name, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(
            ROOT / "missions/AmericasGEO_15deg_90W.script", stage / "AmericasGEO.script"
        )
        (stage / "missions").mkdir()
        for mission in sorted((ROOT / "missions").glob("*.script")):
            shutil.copyfile(mission, stage / "missions" / mission.name)
        for name in ("install.py", "Install.ps1"):
            shutil.copyfile(ROOT / "tools" / name, stage / name)
        entries = {
            p.relative_to(stage).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(stage.rglob("*"))
            if p.is_file()
        }
        (stage / "SHA256.json").write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
        archive = output / f"gmat-mcp-{version}-bundle.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for p in sorted(stage.rglob("*")):
                if p.is_file():
                    bundle.write(p, p.relative_to(stage.parent))
        for artifact in (*stage.glob("*.whl"), *stage.glob("*.tar.gz")):
            shutil.copyfile(artifact, output / artifact.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n")
    return {"bundle": str(archive), "sha256": digest, "version": version}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--uv", default="uv")
    args = parser.parse_args()
    print(json.dumps(build(args.output, args.uv), indent=2))
