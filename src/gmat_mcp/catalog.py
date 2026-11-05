"""Versioned durable index; engine sessions are never resurrected."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .process import process_alive


class Catalog:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "catalog.sqlite")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            raise ValueError("Catalog was created by a newer GMAT MCP")
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, body TEXT NOT NULL); CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, body TEXT NOT NULL); CREATE TABLE IF NOT EXISTS studies(id TEXT PRIMARY KEY, body TEXT NOT NULL); PRAGMA user_version=1;"
        )
        for path in (root / "sessions").glob("*/runs/*/run.json"):
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                continue
            try:
                run = json.loads(path.read_text())
                run["directory"] = str(path.parent.resolve())
                existing = self.run(run["run_id"])
                if (
                    existing
                    and existing.get("status") not in ("queued", "running")
                    and run.get("status") in ("queued", "running")
                ):
                    continue
                if self.interrupted(run):
                    run.update(
                        status="failed",
                        error={
                            "code": "INTERRUPTED",
                            "message": "Server stopped; execution was not replayed",
                        },
                    )
                self.record_run(run)
            except (OSError, ValueError, KeyError):
                continue

    def interrupted(self, run):
        if run.get("status") not in ("queued", "running"):
            return False
        owner = run.get("server_instance")
        if (
            not owner
            or not isinstance(owner, str)
            or len(owner) != 32
            or not all(c in "0123456789abcdef" for c in owner)
        ):
            return True
        try:
            record = json.loads((self.root / "servers" / f"{owner}.json").read_text())
            return not process_alive(int(record["pid"]))
        except (OSError, ValueError, KeyError):
            return True

    def run(self, run_id):
        row = self.db.execute("SELECT body FROM runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def record(self, table: str, identifier: str, body: dict):
        assert table in ("runs", "projects", "studies")
        self.db.execute(
            f"INSERT OR REPLACE INTO {table} VALUES(?,?)",
            (identifier, json.dumps(body, default=str)),
        )
        self.db.commit()

    def record_run(self, run):
        self.record("runs", run["run_id"], run)

    def entries(self, table):
        assert table in ("runs", "projects", "studies")
        return [
            json.loads(r[0])
            for r in self.db.execute(f"SELECT body FROM {table} ORDER BY rowid DESC")
        ]

    def close(self):
        self.db.close()
