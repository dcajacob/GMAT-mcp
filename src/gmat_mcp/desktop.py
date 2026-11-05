"""Opt-in GMAT-Qt bridge client. Disconnect never kills the application."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import uuid
from pathlib import Path
from types import SimpleNamespace

from .errors import GmatError


def records(directory: Path):
    for path in sorted(directory.glob("*.json"))[:100]:
        try:
            if path.is_symlink() or path.stat().st_size > 16384:
                continue
            if os.name == "nt":
                import pywintypes

                from .windows_security import is_private

                try:
                    private = is_private(path)
                except pywintypes.error:
                    private = False
                if not private:
                    continue
            if os.name != "nt" and (
                path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) & 0o077
            ):
                continue
            record = json.loads(path.read_text())
            if (
                record.get("protocol") != 1
                or not isinstance(record.get("port"), int)
                or not 1 <= record["port"] <= 65535
            ):
                continue
            if not isinstance(record.get("token"), str) or len(record["token"]) < 32:
                continue
            yield record
        except (OSError, ValueError, KeyError):
            continue


class DesktopWorker:
    def __init__(self, reader, writer, record, workspace, profile, timeout):
        self.reader, self.writer, self.record = reader, writer, record
        self.workspace = workspace
        workspace.mkdir(parents=True, exist_ok=True)
        self.profile = profile
        self.startup = profile.startup
        self.timeout = timeout
        self.plugins = []
        self.info = {}
        self.state = {}
        self.document_id = None
        self.revision = 0
        self.process = SimpleNamespace(returncode=None)
        self.lock = asyncio.Lock()

    async def rpc(self, operation, **arguments):
        if self.document_id:
            arguments.setdefault("document_id", self.document_id)
        body = (
            json.dumps(
                {"token": self.record["token"], "operation": operation, "arguments": arguments}
            ).encode()
            + b"\n"
        )
        if len(body) > 1024 * 1024:
            raise GmatError("REQUEST_LIMIT", "Desktop message exceeds 1 MiB")
        async with self.lock:
            try:
                async with asyncio.timeout(min(self.timeout, 10)):
                    self.writer.write(body)
                    await self.writer.drain()
                    line = await self.reader.readline()
                if not line:
                    raise ConnectionError()
                reply = json.loads(line)
            except (TimeoutError, ValueError, ConnectionError, OSError) as exc:
                self.process.returncode = -1
                self.writer.close()
                raise GmatError(
                    "DESKTOP_UNAVAILABLE",
                    "Desktop did not respond; completion may be unknown. The application was preserved.",
                ) from exc
            if not reply.get("ok"):
                error = reply.get("error", {})
                raise GmatError(
                    error.get("code", "DESKTOP_ERROR"),
                    error.get("message", "Desktop operation failed"),
                )
            return reply["result"]

    async def refresh(self):
        state = await self.rpc("state")
        if state["document_id"] != self.document_id:
            raise GmatError("DOCUMENT_CHANGED", "Desktop document changed; explicitly reattach")
        self.state = state
        return state

    async def call(self, operation, *, timeout=None, **arguments):
        if operation == "export_script":
            reply = await self.rpc(operation)
            Path(arguments["path"]).write_text(reply["text"], encoding="utf-8")
        elif operation == "execute":
            directory = Path(arguments["output_dir"]).parent
            await self.rpc(
                "start_run",
                expected_version=self.state["model_version"],
                run_id=directory.name,
                directory=str(directory),
                owner="mcp",
            )
            deadline = asyncio.get_running_loop().time() + (timeout or self.timeout)
            try:
                while True:
                    state = await self.refresh()
                    run = state.get("run", {})
                    if run.get("run_id") == directory.name and run.get("status") not in (
                        "queued",
                        "running",
                    ):
                        self.state = state
                        return {
                            "native_status": run.get("native_status", -9),
                            "summary": run.get(
                                "summary", "Desktop run; see engine.log and solver diagnostics"
                            ),
                            "revision": self.revision,
                        }
                    if asyncio.get_running_loop().time() > deadline:
                        await self.rpc("stop")
                        raise GmatError(
                            "TIMEOUT", "Desktop stop requested after timeout; application preserved"
                        )
                    await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                try:
                    await asyncio.wait_for(self.rpc("stop"), 3)
                    async with asyncio.timeout(3):
                        while (await self.refresh()).get("running"):
                            await asyncio.sleep(0.05)
                except Exception:
                    self.completion_unknown = True
                raise
        elif operation == "validate":
            raise GmatError(
                "UNSUPPORTED_CAPABILITY",
                "Use build_desktop to validate the GUI model; initialization occurs on Run",
            )
        else:
            reply = await self.rpc(operation, **arguments)
        return {**reply, "revision": self.revision}

    async def close(self):
        self.writer.close()
        try:
            await asyncio.wait_for(self.writer.wait_closed(), 3)
        except (OSError, TimeoutError):
            self.writer.transport.abort()
        self.process.returncode = 0


class Desktops:
    async def list_desktops(self):
        out = []
        for record in records(self.config.server.desktop_discovery_dir):
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", record["port"], limit=1024 * 1024), 2
                )
                try:
                    writer.write(
                        (
                            json.dumps(
                                {"token": record["token"], "operation": "hello", "arguments": {}}
                            )
                            + "\n"
                        ).encode()
                    )
                    await writer.drain()
                    response = json.loads(await asyncio.wait_for(reader.readline(), 2))
                    if response.get("ok"):
                        out.append(response["result"])
                finally:
                    writer.close()
                    await writer.wait_closed()
            except (OSError, TimeoutError, ValueError):
                continue
        return {"desktops": out}

    async def attach_desktop(self, desktop_id, profile_id):
        from .core import Session

        if self.closing:
            raise GmatError("SHUTTING_DOWN", "Server is closing")
        if len(self.sessions) + self.opening >= self.config.server.max_sessions:
            raise GmatError("SESSION_LIMIT", "Close a session first")
        profile = self.config.profiles.get(profile_id)
        if profile is None or profile.variant != "qt":
            raise GmatError("INVALID_ARGUMENT", "Select a GMAT-Qt profile")
        record = next(
            (
                r
                for r in records(self.config.server.desktop_discovery_dir)
                if r.get("desktop_id") == desktop_id
            ),
            None,
        )
        if record is None:
            raise GmatError("UNKNOWN_DESKTOP", "Enable the Qt MCP bridge and refresh discovery")
        if Path(record["installation_root"]).resolve() != profile.root.resolve():
            raise GmatError(
                "PROFILE_MISMATCH", "Desktop installation differs from the selected profile"
            )
        self.opening += 1
        worker = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", record["port"], limit=1024 * 1024), 3
            )
            sid = uuid.uuid4().hex
            directory = self.config.server.data_dir / "sessions" / sid
            worker = DesktopWorker(
                reader,
                writer,
                record,
                directory / "desktop",
                profile,
                self.config.server.operation_timeout,
            )
            state = await worker.rpc("attach")
            worker.document_id = state["document_id"]
            worker.state = state
            worker.info = state["engine"]
            worker.startup = Path(state["startup"])
            # Profile fingerprints come from files, never from another loaded engine.
            import hashlib

            worker.info["native_libraries"] = [
                {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                for p in profile.bin_dir.glob("*GmatBase*")
                if p.is_file()
            ]
            session = Session(sid, profile_id, profile, directory, worker, backend="desktop")
            session.model_version = state["model_version"]
            session.project_dir = (
                Path(state["path"]).parent if state.get("path") else profile.bin_dir
            )
            self.sessions[sid] = session
            self.desktop_tasks[sid] = asyncio.create_task(self._desktop_watch(sid))
            return {**session.describe(), "desktop": state}
        except BaseException:
            if worker:
                await worker.close()
            raise
        finally:
            self.opening -= 1

    async def _desktop_watch(self, session_id):
        try:
            while session_id in self.sessions and not self.closing:
                await asyncio.sleep(0.25)
                await self.inspect_session(session_id)
        except (GmatError, asyncio.CancelledError):
            pass
        finally:
            self.desktop_tasks.pop(session_id, None)

    async def inspect_session(self, session_id):
        session = self.session(session_id)
        if session.backend == "desktop":
            try:
                state = await session.worker.refresh()
                if state["model_version"] != session.model_version:
                    session.runtime_run = None
                session.model_version = state["model_version"]
                if state.get("project_dir"):
                    session.project_dir = Path(state["project_dir"])
                session.state = "ready"
                self._observe_desktop(session, state)
                return {**session.describe(), "desktop": state}
            except GmatError:
                session.state = "lost"
                raise
        return session.describe()

    def _observe_desktop(self, session, state):
        run = state.get("run", {})
        if not run.get("run_id") or run["run_id"] in self.tasks:
            return
        if run["run_id"] not in self.runs:
            run = {
                **run,
                "session_id": session.id,
                "profile_id": session.profile_id,
                "revision": session.revision,
                "generation": session.generation,
                "created_at": run.get("started_at", ""),
                "collection": session.datasets,
                "server_instance": self.instance_id,
            }
            self.runs[run["run_id"]] = run
            from .core import write_json

            write_json(Path(run["directory"]) / "run.json", run)
            self.catalog.record_run(run)
        else:
            self.runs[run["run_id"]].update(run)
            self.runs[run["run_id"]]["session_id"] = session.id
        if run.get("status") not in ("queued", "running"):
            saved = self.runs[run["run_id"]]
            folder = Path(saved["directory"])
            if not (folder / "provenance.json").exists():
                provenance = {
                    "profile": session.profile.model_dump(mode="json"),
                    "engine": session.worker.info,
                    "model_version": saved.get("model_version"),
                    "collection": session.datasets,
                    "owner": saved.get("owner", "desktop"),
                }
                (folder / "provenance.json").write_text(
                    json.dumps(provenance, indent=2), encoding="utf-8"
                )
            if not saved.get("results_finalized"):
                self._finalize_results(saved)
            (folder / "run.json").write_text(json.dumps(saved, indent=2), encoding="utf-8")
            self.catalog.record_run(saved)
            if run.get("model_version") == state.get("model_version") and not state.get("dirty"):
                session.runtime_run = run["run_id"]
            else:
                session.runtime_run = None

    async def build_desktop(self, session_id, expected_version):
        session = self.session(session_id)
        if session.backend != "desktop":
            raise GmatError("UNSUPPORTED_CAPABILITY", "This session is not attached to a desktop")
        self.available(session)
        async with session.lock:
            self._version(session, expected_version)
            state = await session.worker.rpc("build", expected_version=expected_version)
            session.model_version = state["model_version"]
            session.worker.state = state
            session.runtime_run = None
            return {**session.describe(), "desktop": state}

    async def control_desktop_run(self, run_id, operation):
        run = self.get_run(run_id)
        session = self.session(run["session_id"])
        if session.backend != "desktop":
            raise GmatError("UNSUPPORTED_CAPABILITY", "Pause/resume require a desktop backend")
        return await session.worker.rpc(operation)
