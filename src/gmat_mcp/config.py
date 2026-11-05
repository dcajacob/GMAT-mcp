from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Literal

from platformdirs import user_config_path, user_data_path
from pydantic import BaseModel, ConfigDict, Field

from .errors import GmatError
from .installation import bin_directory, data_directory, installation_root, startup_candidates


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variant: Literal["nasa", "qt"]
    root: Path
    startup_file: Path | None = None
    python_executable: str = sys.executable

    @property
    def bin_dir(self) -> Path:
        return bin_directory(self.root)

    @property
    def data_dir(self) -> Path:
        return data_directory(self.root)

    @property
    def startup(self) -> Path:
        if self.startup_file is not None:
            return self.startup_file
        candidates = startup_candidates(self.root, self.variant)
        return next(
            (path for path in candidates if path.is_file()), self.bin_dir / "gmat_startup_file.txt"
        )


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_dir: Path = Field(default_factory=lambda: user_data_path("gmat-mcp", appauthor=False))
    max_sessions: int = Field(default=4, ge=1, le=64)
    run_timeout: float = Field(default=1800, gt=0, allow_inf_nan=False)
    operation_timeout: float = Field(default=60, gt=0, allow_inf_nan=False)
    desktop_discovery_dir: Path = Field(
        default_factory=lambda: user_data_path("gmat-mcp", appauthor=False) / "desktops"
    )


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server: Settings = Field(default_factory=Settings)
    profiles: dict[str, Profile] = Field(default_factory=dict)


def load_config(path: Path | None = None) -> Config:
    path = path or Path(
        os.environ.get(
            "GMAT_MCP_CONFIG", user_config_path("gmat-mcp", appauthor=False) / "config.toml"
        )
    )
    try:
        with path.open("rb") as stream:
            config = Config.model_validate(tomllib.load(stream))
    except (OSError, ValueError) as exc:
        raise GmatError("CONFIG_ERROR", f"Cannot read configuration {path}: {exc}") from exc
    base = path.resolve().parent

    def absolute(value: Path) -> Path:
        value = Path(os.path.expandvars(str(value))).expanduser()
        return (base / value).resolve() if not value.is_absolute() else value.resolve()

    config.server.data_dir = absolute(config.server.data_dir)
    config.server.desktop_discovery_dir = absolute(config.server.desktop_discovery_dir)
    for profile in config.profiles.values():
        profile.root = installation_root(absolute(profile.root))
        if profile.startup_file:
            profile.startup_file = absolute(profile.startup_file)
        executable = os.path.expandvars(os.path.expanduser(profile.python_executable))
        if "/" in executable or "\\" in executable:
            # Keep a venv interpreter's symlink: resolving it changes its environment.
            executable = os.path.abspath(base / executable)
        profile.python_executable = executable
    return config
