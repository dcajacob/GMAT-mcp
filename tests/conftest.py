from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

from gmat_mcp.config import Config, Profile, Settings, load_config
from gmat_mcp.core import Manager


@pytest.fixture
def fake_config(tmp_path):
    root = tmp_path / "fake gmat"
    package = root / "bin/gmatpy"
    versioned = package / f"_py{sys.version_info.major}{sys.version_info.minor}"
    versioned.mkdir(parents=True)
    (versioned / "__init__.py").write_text("")
    (versioned / "gmat_py.py").write_text("import sys\n_gmat_py = sys.modules['gmatpy']\n")
    (package / "__init__.py").write_text(Path(__file__).with_name("fake_engine.py").read_text())
    (root / "bin/gmat_startup_file.txt").write_text("ROOT_PATH = ../\nOUTPUT_PATH = ../output/\n")
    return Config(
        server=Settings(data_dir=tmp_path / "artifacts"),
        profiles={"fake": Profile(variant="nasa", root=root, python_executable=sys.executable)},
    )


@pytest_asyncio.fixture
async def manager(fake_config):
    manager = Manager(fake_config)
    try:
        yield manager
    finally:
        await manager.shutdown()


@pytest.fixture(scope="session")
def native_config():
    path = os.environ.get("GMAT_MCP_TEST_CONFIG")
    if not path:
        pytest.skip("Set GMAT_MCP_TEST_CONFIG to test actual installations")
    config = load_config(Path(path))
    if {p.variant for p in config.profiles.values()} != {"nasa", "qt"}:
        pytest.fail("Native qualification requires NASA and GMAT-Qt profiles")
    return config
