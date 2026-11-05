"""The finite backend contract. No native pointers cross this interface."""

from pathlib import Path
from typing import Protocol


class Backend(Protocol):
    workspace: Path
    startup: Path
    info: dict
    plugins: list

    async def call(self, operation: str, *, timeout: float | None = None, **arguments) -> dict: ...
    async def close(self) -> None: ...
