"""CI asset caching must never accept a different GMAT distribution."""

import hashlib
import importlib.util
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "macos_preparation", Path(__file__).resolve().parents[1] / "tools/prepare_macos.py"
)
preparation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preparation)


def test_verified_cache_avoids_network(tmp_path, monkeypatch):
    path = tmp_path / "nasa.dmg"
    path.write_bytes(b"verified image")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(preparation, "run", lambda *a, **kw: pytest.fail("Cache invoked network"))
    result = preparation.download("https://official.invalid", path, expected_sha256=digest)
    assert result["cached"] and result["sha256"] == digest


def test_slow_mirror_falls_back_and_atomically_replaces_bad_cache(tmp_path, monkeypatch):
    path = tmp_path / "nasa.dmg"
    path.write_bytes(b"bad cached image")
    contents = b"correct image"
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        partial = Path(args[args.index("--output") + 1])
        partial.write_bytes(contents)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(28, args, stderr="Mirror below minimum speed")
        return subprocess.CompletedProcess(args, 0, stdout="2000000", stderr="")

    monkeypatch.setattr(preparation, "run", run)
    result = preparation.download(
        "https://official.invalid",
        path,
        expected_sha256=hashlib.sha256(contents).hexdigest(),
        fallbacks=("https://official-mirror.invalid",),
    )
    assert not result["cached"] and len(calls) == 2
    assert result["mirror"] == "https://official-mirror.invalid"
    assert path.read_bytes() == contents
    assert not path.with_suffix(".dmg.part").exists()
    assert "--speed-time" in calls[0] and "--max-time" in calls[0]


def test_hash_mismatch_preserves_existing_file_and_removes_partial(tmp_path, monkeypatch):
    path = tmp_path / "nasa.dmg"
    path.write_bytes(b"previous image")

    def run(*args, **kwargs):
        Path(args[args.index("--output") + 1]).write_bytes(b"unexpected distribution")
        return subprocess.CompletedProcess(args, 0, stdout="2000000", stderr="")

    monkeypatch.setattr(preparation, "run", run)
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        preparation.download("https://official.invalid", path, expected_sha256="0" * 64)
    assert path.read_bytes() == b"previous image"
    assert not path.with_suffix(".dmg.part").exists()
