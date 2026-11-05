from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .backends import Backend
from .capabilities import Capabilities
from .config import Config, Profile
from .discovery import probe_interpreter
from .errors import GmatError
from .process import prepare_loader, spawn_options, terminate, worker_environment
from .startup import prepare_startup

MAX_READ = 65536


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: object):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def bounded_read(path: Path, offset: int = 0, limit: int = 16384) -> dict:
    if offset < 0 or not 1 <= limit <= MAX_READ:
        raise GmatError("INVALID_RANGE", f"offset must be >= 0; limit must be 1..{MAX_READ}")
    try:
        with path.open("rb") as stream:
            stream.seek(offset)
            content = stream.read(limit)
            more = bool(stream.read(1))
    except OSError as exc:
        raise GmatError("ARTIFACT_ERROR", str(exc)) from exc
    try:
        text = content.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        text = base64.b64encode(content).decode("ascii")
        encoding = "base64"
    return {
        "content": text,
        "encoding": encoding,
        "offset": offset,
        "next_offset": offset + len(content),
        "has_more": more,
    }


@dataclass
class Worker:
    process: asyncio.subprocess.Process
    workspace: Path
    info: dict
    startup: Path
    plugins: list[str]
    timeout: float
    job: object | None = None

    @classmethod
    async def start(cls, profile: Profile, workspace: Path, timeout: float) -> Worker:
        workspace.mkdir(parents=True)
        loader, aliases = prepare_loader(profile.bin_dir, workspace)
        startup, plugins = prepare_startup(profile.startup, profile.bin_dir, workspace, aliases)
        interpreter = await asyncio.to_thread(probe_interpreter, profile.python_executable)
        if interpreter.get("error"):
            raise GmatError(
                "ENGINE_SETUP",
                f"Cannot launch a compatible worker with {profile.python_executable}: {interpreter['error']}. "
                "Select matching CPython 3.11+ with discover --python.",
            )
        stderr = (workspace / "native.log").open("ab", buffering=0)
        try:
            environment = worker_environment(profile.bin_dir, loader)
            environment["GMAT_MCP_START_GATE"] = "1"
            process = await asyncio.create_subprocess_exec(
                profile.python_executable,
                "-u",
                "-B",
                str(Path(__file__).with_name("worker.py")),
                str(profile.bin_dir),
                str(startup),
                cwd=workspace,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
                env=environment,
                limit=16 * 1024 * 1024,
                **spawn_options(),
            )
        except OSError as exc:
            raise GmatError(
                "ENGINE_SETUP", f"Cannot launch {profile.python_executable}: {exc}"
            ) from exc
        finally:
            stderr.close()
        worker = cls(process, workspace, {}, startup, plugins, timeout)
        try:
            if sys.platform == "win32":
                from .windows_job import WindowsJob

                try:
                    worker.job = WindowsJob(process.pid)
                except OSError as exc:
                    raise GmatError(
                        "ENGINE_SETUP", f"Cannot contain Windows worker process tree: {exc}"
                    ) from exc
            process.stdin.write(b'{"operation":"_start"}\n')
            await process.stdin.drain()
            worker.info = await worker.receive(timeout)
        except BaseException:
            await worker.close()
            raise
        return worker

    async def receive(self, timeout: float) -> dict:
        try:
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout)
            if not line:
                await self.close()
                raise GmatError("WORKER_LOST", f"GMAT worker exited ({self.process.returncode})")
            message = json.loads(line)
        except TimeoutError as exc:
            await self.close()
            raise GmatError("TIMEOUT", f"GMAT operation exceeded {timeout:g} seconds") from exc
        except (ValueError, OSError) as exc:
            await self.close()
            raise GmatError("WORKER_LOST", f"Invalid worker response: {exc}") from exc
        if not message["ok"]:
            error = message["error"]
            raise GmatError(error["code"], error["message"], revision=message.get("revision"))
        return message["result"]

    async def call(self, operation: str, *, timeout: float | None = None, **arguments) -> dict:
        if self.process.returncode is not None:
            raise GmatError("WORKER_LOST", f"GMAT worker exited ({self.process.returncode})")
        try:
            self.process.stdin.write(
                (json.dumps({"operation": operation, "arguments": arguments}) + "\n").encode()
            )
            await self.process.stdin.drain()
        except (OSError, ConnectionError) as exc:
            await self.close()
            raise GmatError("WORKER_LOST", f"Worker connection failed: {exc}") from exc
        return await self.receive(timeout or self.timeout)

    async def close(self):
        if self.job is not None:
            self.job.close()
        await terminate(self.process)
        if self.process.stdin is not None:
            self.process.stdin.close()


@dataclass
class Session:
    id: str
    profile_id: str
    profile: Profile
    directory: Path
    worker: Backend
    state: str = "ready"
    revision: int = 0
    generation: int = 1
    runtime_run: str | None = None
    active_run: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    model_version: str = field(default_factory=lambda: uuid.uuid4().hex)
    backend: str = "worker"
    project_dir: Path | None = None
    datasets: list = field(default_factory=list)
    history: list = field(default_factory=list)
    future: list = field(default_factory=list)

    def describe(self) -> dict:
        if self.worker.process.returncode is not None:
            self.state = "lost"
        return {
            "session_id": self.id,
            "profile_id": self.profile_id,
            "state": self.state,
            "revision": self.revision,
            "generation": self.generation,
            "active_run": self.active_run,
            "runtime_run": self.runtime_run,
            "engine": self.worker.info,
            "directory": str(self.directory),
            "model_version": self.model_version,
            "backend": self.backend,
        }


class Manager(Capabilities):
    def __init__(self, config: Config):
        self.config = config
        self.sessions: dict[str, Session] = {}
        self.runs: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.opening = 0
        self.closing = False
        self._init_capabilities()

    def list_profiles(self) -> dict:
        profiles = []
        for name, profile in self.config.profiles.items():
            profiles.append(
                {
                    "id": name,
                    **profile.model_dump(mode="json"),
                    "startup_file": str(profile.startup),
                    "bindings": sorted(p.name for p in (profile.bin_dir / "gmatpy").glob("_py*")),
                    "configured": profile.startup.is_file(),
                }
            )
        return {"profiles": profiles, "max_sessions": self.config.server.max_sessions}

    def session(self, session_id: str) -> Session:
        if session_id not in self.sessions:
            raise GmatError("UNKNOWN_SESSION", f"Unknown session: {session_id}")
        return self.sessions[session_id]

    def available(self, session: Session):
        if self.closing:
            raise GmatError("SHUTTING_DOWN", "Server is shutting down")
        if session.lock.locked():
            raise GmatError(
                "SESSION_BUSY", "Session is busy; status, logs and artifacts remain available"
            )
        if session.state != "ready":
            if session.backend == "desktop":
                raise GmatError(
                    "SESSION_LOST",
                    "Detach and explicitly reattach the desktop; its application was preserved",
                )
            raise GmatError("SESSION_LOST", "Reset the session or reload a saved script")

    async def create_session(self, profile_id: str) -> dict:
        if self.closing:
            raise GmatError("SHUTTING_DOWN", "Server is shutting down")
        if profile_id not in self.config.profiles:
            raise GmatError("UNKNOWN_PROFILE", f"Unknown installation profile: {profile_id}")
        if len(self.sessions) + self.opening >= self.config.server.max_sessions:
            raise GmatError("SESSION_LIMIT", "Close a session before opening another")
        self.opening += 1
        session_id = uuid.uuid4().hex
        directory = self.config.server.data_dir / "sessions" / session_id
        profile = self.config.profiles[profile_id]
        try:
            worker = await Worker.start(
                profile,
                directory / "workers" / uuid.uuid4().hex,
                self.config.server.operation_timeout,
            )
            if self.closing:
                await worker.close()
                raise GmatError("SHUTTING_DOWN", "Server shut down while this engine was opening")
            session = Session(session_id, profile_id, profile, directory, worker)
            self.sessions[session_id] = session
            return session.describe()
        except GmatError as exc:
            exc.details["diagnostics"] = str(directory)
            raise
        finally:
            self.opening -= 1

    async def call(
        self, session_id: str, operation: str, *, mutates: bool = False, **arguments
    ) -> dict:
        session = self.session(session_id)
        self.available(session)
        if session.backend == "desktop":
            await self.inspect_session(session_id)
            self.available(session)
        async with session.lock:
            try:
                if arguments.get("view") == "runtime" and session.runtime_run is None:
                    raise GmatError(
                        "RUNTIME_UNAVAILABLE",
                        "Run the current model before requesting runtime state",
                    )
                if mutates:
                    if session.backend == "desktop":
                        raise GmatError(
                            "EXPECTED_VERSION_REQUIRED",
                            "Use edit_mission with expected_version for desktop mutations",
                        )
                    session.runtime_run = None
                    session.model_version = uuid.uuid4().hex
                    session.history.clear()
                    session.future.clear()
                result = await session.worker.call(operation, **arguments)
                session.revision = result.get("revision", session.revision)
                if arguments.get("view") == "runtime":
                    result["run_id"] = session.runtime_run
                result["session_id"] = session_id
                result["generation"] = session.generation
                return result
            except GmatError as exc:
                if exc.details.get("revision") is not None:
                    session.revision = exc.details["revision"]
                if exc.code in (
                    "TIMEOUT",
                    "WORKER_LOST",
                    "DESKTOP_UNAVAILABLE",
                    "DOCUMENT_CHANGED",
                ):
                    session.state = "lost"
                raise
            except asyncio.CancelledError:
                await session.worker.close()
                session.state = "lost"
                raise

    async def reset_session(self, session_id: str) -> dict:
        session = self.session(session_id)
        if session.backend == "desktop":
            raise GmatError(
                "UNSUPPORTED_CAPABILITY",
                "Detach and explicitly reattach; reset never discards GUI work",
            )
        if session.lock.locked():
            raise GmatError("SESSION_BUSY", "Cancel the active run before resetting")
        async with session.lock:
            worker = await Worker.start(
                session.profile,
                session.directory / "workers" / uuid.uuid4().hex,
                self.config.server.operation_timeout,
            )
            await session.worker.close()
            session.worker = worker
            session.state = "ready"
            session.generation += 1
            session.revision = 0
            session.runtime_run = None
            session.datasets = []
            session.history.clear()
            session.future.clear()
            session.model_version = uuid.uuid4().hex
        return session.describe()

    async def close_session(self, session_id: str) -> dict:
        session = self.session(session_id)
        observer = self.desktop_tasks.pop(session_id, None)
        if observer:
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
        if session.active_run:
            await self.cancel_run(session.active_run)
        if session.lock.locked():
            raise GmatError("SESSION_BUSY", "Wait for the active operation before closing")
        async with session.lock:
            await session.worker.close()
            del self.sessions[session_id]
        return {"session_id": session_id, "closed": True}

    async def load_script(
        self,
        session_id: str,
        *,
        text: str | None = None,
        path: str | None = None,
        project_dir: str | None = None,
        expected_version: str | None = None,
    ) -> dict:
        session = self.session(session_id)
        if session.lock.locked():
            raise GmatError("SESSION_BUSY", "Session is busy")
        if (text is None) == (path is None):
            raise GmatError("INVALID_ARGUMENT", "Supply exactly one of text or path")
        source = Path(path).expanduser().resolve() if path is not None else None
        project = (
            Path(project_dir).expanduser().resolve()
            if project_dir
            else (source.parent if source else session.directory)
        )
        if not project.is_dir():
            raise GmatError("INVALID_ARGUMENT", f"Project directory does not exist: {project}")
        if session.backend == "desktop":
            async with session.lock:
                self._version(session, expected_version)
                snapshot = await self._snapshot(session)
                snapshot.update(
                    text=source.read_text(encoding="utf-8") if source else text,
                    project_dir=str(project),
                    datasets=[],
                )
                from .results import collection, embedded_spec

                parsed_spec = embedded_spec(snapshot["text"])
                if parsed_spec:
                    snapshot["datasets"] = collection(parsed_spec)
                return await self._stage(session, snapshot)
        async with session.lock:
            worker = await Worker.start(
                session.profile,
                session.directory / "workers" / uuid.uuid4().hex,
                self.config.server.operation_timeout,
            )
            try:
                if source is None:
                    source = worker.workspace / "import.script"
                    source.write_text(text, encoding="utf-8")
                result = await worker.call(
                    "load_script", path=str(source), project_dir=str(project)
                )
                datasets = []
                if result.get("results_spec") is not None:
                    from .results import collection

                    datasets = collection(result["results_spec"])
                    names = (await worker.call("list_objects"))["objects"]
                    if any(d["report"] not in names for d in datasets):
                        raise GmatError(
                            "RESULTS_CONFIG_ERROR",
                            "An embedded results specification references missing ReportFile resources",
                        )
                    columns = [c for d in datasets for c in d["columns"]]
                    native = await worker.call(
                        "parameter_metadata", expressions=[c["expression"] for c in columns]
                    )
                    for col, meta in zip(columns, native["parameters"]):
                        col.update(meta)
            except BaseException as exc:
                await worker.close()
                if isinstance(exc, GmatError):
                    exc.details["diagnostics"] = bounded_read(
                        worker.workspace / "native.log", limit=MAX_READ
                    )
                raise
            await session.worker.close()
            session.worker = worker
            session.generation += 1
            session.revision = result["revision"]
            session.runtime_run = None
            session.state = "ready"
            session.project_dir = project
            session.datasets = datasets
            session.history.clear()
            session.future.clear()
            session.model_version = uuid.uuid4().hex
            return {
                **result,
                "session_id": session_id,
                "generation": session.generation,
                "model_version": session.model_version,
            }

    async def export_script(
        self, session_id: str, path: str | None = None, overwrite: bool = False
    ) -> dict:
        session = self.session(session_id)
        # Always ask GMAT to write inside its own workspace, then explicitly copy on request.
        result = await self.call(
            session_id, "export_script", path=str(session.worker.workspace / "export.script")
        )
        if path:
            destination = Path(path).expanduser().resolve()
            try:
                with destination.open("w" if overwrite else "x", encoding="utf-8") as stream:
                    stream.write(result["text"])
            except OSError as exc:
                raise GmatError("SAVE_ERROR", str(exc)) from exc
            result["path"] = str(destination)
        return result

    async def run_mission(self, session_id: str, timeout: float | None = None) -> dict:
        session = self.session(session_id)
        if session.backend == "desktop":
            state = await session.worker.refresh()
            if state["running"]:
                raise GmatError("SESSION_BUSY", "The desktop is already running a mission")
            if state["model_version"] != session.model_version:
                raise GmatError(
                    "STALE_VERSION", "Inspect the changed desktop mission before running"
                )
        self.available(session)
        if timeout is not None and (not 0 < timeout <= self.config.server.run_timeout):
            raise GmatError(
                "INVALID_ARGUMENT",
                "timeout must be positive and no larger than configured run_timeout",
            )
        run_id = uuid.uuid4().hex
        directory = session.directory / "runs" / run_id
        directory.mkdir(parents=True)
        await session.lock.acquire()
        run = {
            "run_id": run_id,
            "session_id": session_id,
            "profile_id": session.profile_id,
            "status": "queued",
            "created_at": now(),
            "revision": session.revision,
            "generation": session.generation,
            "directory": str(directory),
            "model_version": session.model_version,
            "collection": session.datasets,
            "server_instance": self.instance_id,
        }
        try:
            write_json(directory / "run.json", run)
            self.catalog.record_run(run)
        except Exception as exc:
            session.lock.release()
            raise GmatError("PERSISTENCE_ERROR", f"Cannot record queued run: {exc}") from exc
        self.runs[run_id] = run
        session.active_run = run_id
        session.runtime_run = None
        self.tasks[run_id] = asyncio.create_task(
            self._execute_run(session, run, timeout or self.config.server.run_timeout)
        )
        return dict(run)

    async def _execute_run(self, session: Session, run: dict, timeout: float):
        directory = Path(run["directory"])
        legacy_output = session.worker.workspace / "output"
        original_outputs = {}
        try:
            original_outputs = {
                path: (path.stat().st_mtime_ns, path.stat().st_size)
                for path in legacy_output.rglob("*")
                if path.is_file() and not path.is_symlink()
            }
            run.update(status="running", started_at=now())
            write_json(directory / "run.json", run)
            self.catalog.record_run(run)
            (directory / "output").mkdir()
            snapshot = directory / "mission.script"
            await session.worker.call("export_script", path=str(snapshot))
            effective = session.worker.startup.read_text(encoding="utf-8")
            effective += "\n# FileManager overrides applied before execution:\n"
            for key in ("OUTPUT_PATH", "MEASUREMENT_PATH", "VEHICLE_EPHEM_PATH"):
                effective += f"{key} = {(directory / 'output').as_posix()}/\n"
            effective += f"LOG_FILE = {(directory / 'engine.log').as_posix()}\n"
            (directory / "effective_startup.txt").write_text(effective, encoding="utf-8")
            write_json(
                directory / "provenance.json",
                {
                    "profile": session.profile.model_dump(mode="json"),
                    "engine": session.worker.info,
                    "plugins": session.worker.plugins,
                    "mission_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                    "startup_sha256": hashlib.sha256(
                        (directory / "effective_startup.txt").read_bytes()
                    ).hexdigest(),
                    "server_python": sys.version,
                    "revision": session.revision,
                    "model_version": session.model_version,
                    "collection": session.datasets,
                },
            )
            result = await session.worker.call(
                "execute",
                timeout=timeout,
                output_dir=str(directory / "output"),
                log_file=str(directory / "engine.log"),
            )
            run.update(result)
            run["status"] = "succeeded" if result["native_status"] == 1 else "failed"
            if session.backend == "desktop" and result["native_status"] == -4:
                run["status"] = "cancelled"
            session.runtime_run = run["run_id"]
        except asyncio.CancelledError:
            run["status"] = "cancelled"
            if session.backend == "desktop" and getattr(
                session.worker, "completion_unknown", False
            ):
                run["error"] = {
                    "code": "DESKTOP_COMPLETION_UNKNOWN",
                    "message": "Cancellation requested; desktop completion could not be confirmed. Application preserved.",
                }
            session.state = "lost"
            await session.worker.close()
        except GmatError as exc:
            run["status"] = "timed_out" if exc.code == "TIMEOUT" else "failed"
            run["error"] = exc.as_dict()
            if exc.code in ("TIMEOUT", "WORKER_LOST", "DESKTOP_UNAVAILABLE", "DOCUMENT_CHANGED"):
                session.state = "lost"
        except Exception as exc:
            run.update(status="failed", error={"code": "SERVER_ERROR", "message": str(exc)})
            session.state = "lost"
            await session.worker.close()
        finally:
            run["finished_at"] = now()
            try:
                native_log = session.worker.workspace / "native.log"
                if native_log.exists():
                    shutil.copyfile(native_log, directory / "worker.log")
                elif session.backend == "desktop" and (directory / "engine.log").exists():
                    shutil.copyfile(directory / "engine.log", directory / "worker.log")
                # Some plugins cache paths at parse time. Preserve newly produced
                # default-output files even when they ignore FileManager's override.
                for path in legacy_output.rglob("*"):
                    if (
                        not path.is_file()
                        or path.is_symlink()
                        or not path.resolve().is_relative_to(legacy_output)
                    ):
                        continue
                    signature = (path.stat().st_mtime_ns, path.stat().st_size)
                    if original_outputs.get(path) != signature:
                        destination = directory / "output" / path.relative_to(legacy_output)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        if not destination.exists():
                            shutil.copy2(path, destination)
            except OSError as exc:
                run["artifact_error"] = str(exc)
            try:
                await asyncio.to_thread(self._finalize_results, run)
                write_json(directory / "run.json", run)
                self.catalog.record_run(run)
            except OSError as exc:
                run["artifact_error"] = str(exc)
            finally:
                session.active_run = None
                session.lock.release()
                self.tasks.pop(run["run_id"], None)

    def get_run(self, run_id: str) -> dict:
        if run_id not in self.runs:
            saved = self.catalog.run(run_id)
            if saved:
                self.runs[run_id] = saved
            else:
                raise GmatError("UNKNOWN_RUN", f"Unknown run: {run_id}")
        if self.runs[run_id].get("server_instance") not in (None, self.instance_id):
            self.runs[run_id] = self.catalog.run(run_id) or self.runs[run_id]
        value = dict(self.runs[run_id])
        if run_id in self.tasks and value["status"] not in ("queued", "running"):
            value.update(
                execution_status=value["status"], status="running", results_status="processing"
            )
        return value

    async def cancel_run(self, run_id: str) -> dict:
        run = self.get_run(run_id)
        task = self.tasks.get(run_id)
        if task is None and run["status"] in ("queued", "running"):
            session = self.sessions.get(run["session_id"])
            if session and session.backend == "desktop":
                await session.worker.rpc("stop")
                self.runs[run_id]["cancellation_requested"] = True
                await self.inspect_session(session.id)
        if task:
            # Let a freshly queued task enter its try/finally before cancellation.
            await asyncio.sleep(0)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return self.get_run(run_id)

    def get_logs(self, session_id: str, offset: int = 0, limit: int = 16384) -> dict:
        session = self.session(session_id)
        return bounded_read(session.worker.workspace / "native.log", offset, limit)

    def list_artifacts(self, run_id: str) -> dict:
        directory = Path(self.get_run(run_id)["directory"])
        artifacts = []
        for path in sorted(directory.rglob("*")):
            if (
                not path.is_file()
                or path.is_symlink()
                or not path.resolve().is_relative_to(directory)
            ):
                continue
            relative = path.relative_to(directory).as_posix()
            artifact_id = hashlib.sha256(relative.encode()).hexdigest()[:32]
            artifacts.append(
                {
                    "id": artifact_id,
                    "path": relative,
                    "size": path.stat().st_size,
                    "uri": f"gmat://runs/{run_id}/artifacts/{artifact_id}",
                }
            )
            if len(artifacts) >= 1000:
                break
        return {"run_id": run_id, "artifacts": artifacts}

    def read_artifact(
        self, run_id: str, artifact_id: str, offset: int = 0, limit: int = 16384
    ) -> dict:
        directory = Path(self.get_run(run_id)["directory"])
        for artifact in self.list_artifacts(run_id)["artifacts"]:
            if artifact["id"] == artifact_id:
                path = directory / artifact["path"]
                if not path.resolve().is_relative_to(directory) or path.is_symlink():
                    break
                return {**artifact, **bounded_read(path, offset, limit)}
        raise GmatError("UNKNOWN_ARTIFACT", "Artifact does not exist in this run")

    async def shutdown(self):
        self.closing = True
        for task in list(self.desktop_tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self.desktop_tasks.values()), return_exceptions=True)
        await self.stop_studies()
        await asyncio.gather(
            *(self.cancel_run(run_id) for run_id in list(self.tasks)), return_exceptions=True
        )
        await asyncio.gather(
            *(session.worker.close() for session in self.sessions.values()), return_exceptions=True
        )
        self.catalog.close()
        self.owner_record.unlink(missing_ok=True)
