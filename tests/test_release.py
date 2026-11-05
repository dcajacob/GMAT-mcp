"""Bundle integrity must be checked before creating an installation environment."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "release_installer", Path(__file__).resolve().parents[1] / "tools/install.py"
)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_release_tampering_and_unsafe_inventory_preserve_destination(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    wheel = bundle / "gmat_mcp-0.3.0-py3-none-any.whl"
    wheel.write_bytes(b"a wheel")
    lock = bundle / "requirements.lock"
    lock.write_bytes(b"locked")
    entries = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (wheel, lock)}
    inventory = bundle / "SHA256.json"
    inventory.write_text(json.dumps(entries))
    assert installer.verify(bundle) == wheel
    lock.write_bytes(b"tampered")
    destination = tmp_path / "environment"
    with pytest.raises(ValueError, match="checksum"):
        installer.install(bundle, destination)
    assert not destination.exists()
    inventory.write_text(json.dumps({"../escape.whl": "0" * 64}))
    with pytest.raises(ValueError, match="unsafe"):
        installer.verify(bundle)
