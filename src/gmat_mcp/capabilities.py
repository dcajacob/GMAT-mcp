"""Transactional workflows layered over the isolated backend."""

from __future__ import annotations

import asyncio
import copy
import difflib
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

from . import results
from .catalog import Catalog
from .desktop import Desktops
from .editing import edit_commands, remove_managed_reports
from .errors import GmatError
from .studies import Studies


class Capabilities(Studies, Desktops):
    def _init_capabilities(self):
        self.catalog = Catalog(self.config.server.data_dir)
        self.runs.update((r["run_id"], r) for r in self.catalog.entries("runs"))
        for run in self.runs.values():
            if self.catalog.interrupted(run):
                run.update(
                    status="failed",
                    error={
                        "code": "INTERRUPTED",
                        "message": "Server stopped; execution was not replayed",
                    },
                )
                self.catalog.record_run(run)
        self.instance_id = uuid.uuid4().hex
        owner_dir = self.config.server.data_dir / "servers"
        owner_dir.mkdir(exist_ok=True)
        self.owner_record = owner_dir / f"{self.instance_id}.json"
        self.owner_record.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        self.desktop_tasks = {}
        self.study_tasks = {}
        self.studies = {s["study_id"]: s for s in self.catalog.entries("studies")}
        for study in self.studies.values():
            if self.catalog.interrupted(study):
                study.update(status="interrupted", stop_reason="server_restart")
                self.catalog.record("studies", study["study_id"], study)
        self._study_slots = asyncio.Semaphore(min(4, self.config.server.max_sessions))
        self._plot_lock = asyncio.Lock()

    def _version(self, session, expected):
        if expected != session.model_version:
            raise GmatError(
                "STALE_VERSION", "The mission changed; inspect the current model before editing"
            )

    async def _snapshot(self, session):
        data = await session.worker.call(
            "export_script", path=str(session.worker.workspace / "snapshot.script")
        )
        if session.backend == "desktop":
            if data.get("dirty"):
                raise GmatError(
                    "UNBUILT_EDITOR",
                    "Build the current desktop editor before taking a configured snapshot",
                )
            if data.get("model_version") != session.model_version:
                raise GmatError(
                    "STALE_VERSION", "Inspect the changed desktop mission before taking a snapshot"
                )
        return {
            "text": data["text"],
            "datasets": copy.deepcopy(session.datasets),
            "project_dir": str(session.project_dir or session.directory),
        }

    async def _stage(
        self, session, snapshot, transform=None, validation="parse", dry_run=False, remember=True
    ):
        from .core import Worker

        if session.backend == "desktop" and validation == "initialize":
            raise GmatError(
                "UNSUPPORTED_CAPABILITY",
                "Desktop edits support parse validation; object initialization is checked by the GUI Run",
            )
        if validation not in ("parse", "initialize"):
            raise GmatError("INVALID_ARGUMENT", "validation must be parse or initialize")
        worker = await Worker.start(
            session.profile,
            session.directory / "workers" / uuid.uuid4().hex,
            self.config.server.operation_timeout,
        )
        try:
            path = worker.workspace / "candidate.script"
            path.write_text(snapshot["text"], encoding="utf-8")
            await worker.call("load_script", path=str(path), project_dir=snapshot["project_dir"])
            if transform:
                await transform(worker)
            if snapshot["datasets"]:
                columns = [c for d in snapshot["datasets"] for c in d["columns"]]
                native = await worker.call(
                    "parameter_metadata", expressions=[c["expression"] for c in columns]
                )
                for col, meta in zip(columns, native["parameters"]):
                    col.update(meta)
            checked = "not_checked"
            if validation == "initialize":
                await worker.call("validate")
                checked = "passed"
            exported = await worker.call(
                "export_script", path=str(worker.workspace / "candidate-export.script")
            )
            if session.backend == "desktop":
                directives = [
                    line
                    for line in snapshot["text"].splitlines()
                    if line.lstrip().startswith("% GMAT-Qt-")
                    and line not in exported["text"].splitlines()
                ]
                if directives:
                    exported["text"] = "\n".join(directives) + "\n" + exported["text"]
            before = await self._snapshot(session)
            diff = "".join(
                difflib.unified_diff(
                    before["text"].splitlines(True),
                    exported["text"].splitlines(True),
                    fromfile="current.script",
                    tofile="candidate.script",
                )
            )
            if not dry_run:
                if session.backend == "desktop":
                    state = await session.worker.rpc(
                        "apply_script",
                        text=exported["text"],
                        expected_version=session.model_version,
                        project_dir=snapshot["project_dir"],
                    )
                    session.worker.state = state
                if remember:
                    session.history.append(before)
                    session.history = session.history[-20:]
                    session.future.clear()
                if session.backend == "worker":
                    await session.worker.close()
                    session.worker = worker
                    worker = None
                session.generation += 1
                session.revision = exported["revision"]
                session.runtime_run = None
                session.model_version = (
                    state["model_version"] if session.backend == "desktop" else uuid.uuid4().hex
                )
                session.datasets = copy.deepcopy(snapshot["datasets"])
                session.project_dir = Path(snapshot["project_dir"])
            return {
                "committed": not dry_run,
                "diff": diff,
                "parsing": "passed",
                "object_initialization": checked,
                "model_version": session.model_version,
                "generation": session.generation,
            }
        except GmatError as exc:
            if worker:
                from .core import bounded_read

                exc.details["diagnostics"] = bounded_read(worker.workspace / "native.log")
            raise
        finally:
            if worker:
                await worker.close()

    async def configure_results(self, session_id, spec, expected_version=None):
        session = self.session(session_id)
        self.available(session)
        datasets = results.collection(spec)
        async with session.lock:
            if session.backend == "desktop" or expected_version is not None:
                self._version(session, expected_version)
            existing = (await session.worker.call("list_objects"))["objects"]
            owned = {d["report"] for d in session.datasets}
            if any(d["report"] in existing and d["report"] not in owned for d in datasets):
                raise GmatError(
                    "RESULT_NAME_COLLISION",
                    "A requested managed ReportFile name is already owned by the mission",
                )
            snapshot = await self._snapshot(session)
            text = remove_managed_reports(snapshot["text"], [d["report"] for d in session.datasets])
            text = results.report_script(datasets) + "\n" + text
            text = results.with_spec(text, spec)
            snapshot.update(text=text, datasets=datasets)

            async def metadata(worker):
                columns = [c for d in datasets for c in d["columns"]]
                data = await worker.call(
                    "parameter_metadata", expressions=[c["expression"] for c in columns]
                )
                for col, native in zip(columns, data["parameters"]):
                    col.update(native)

            reply = await self._stage(
                session,
                snapshot,
                metadata,
                validation="parse" if session.backend == "desktop" else "initialize",
            )
            return {**reply, "datasets": session.datasets}

    async def edit_mission(
        self, session_id, expected_version, changes, dry_run=False, validation="parse"
    ):
        session = self.session(session_id)
        self.available(session)
        async with session.lock:
            self._version(session, expected_version)
            snapshot = await self._snapshot(session)
            command_changes = [c for c in changes if c.get("op", "").endswith("_command")]
            if command_changes:
                tree = (await session.worker.call("list_commands"))["commands"]
                snapshot["text"] = edit_commands(snapshot["text"], tree, command_changes)

            async def apply(worker):
                for change in changes:
                    op = change.get("op")
                    if op == "set_field":
                        await worker.call(
                            "set_field",
                            name=change["name"],
                            field=change["field"],
                            value=change["value"],
                        )
                    elif op == "create_object":
                        await worker.call(
                            "create_object",
                            type=change["type"],
                            name=change["name"],
                            constructor_args=change.get("constructor_args"),
                        )
                    elif change not in command_changes:
                        raise GmatError("INVALID_ARGUMENT", f"Unsupported edit: {op}")

            return await self._stage(session, snapshot, apply, validation, dry_run)

    async def restore_edit(self, session_id, expected_version, redo=False):
        session = self.session(session_id)
        self.available(session)
        async with session.lock:
            self._version(session, expected_version)
            source = session.future if redo else session.history
            if not source:
                raise GmatError("NO_HISTORY", "No snapshot available")
            snapshot = source[-1]
            current = await self._snapshot(session)
            reply = await self._stage(session, snapshot, remember=False)
            source.pop()
            (session.history if redo else session.future).append(current)
            return reply

    def _finalize_results(self, run):
        directory = Path(run["directory"])
        try:
            if run.get("collection"):
                provenance_path = directory / "provenance.json"
                provenance = (
                    json.loads(provenance_path.read_text()) if provenance_path.exists() else {}
                )
                summary = results.ingest(
                    directory,
                    run["collection"],
                    {
                        **{
                            k: run[k] for k in ("run_id", "profile_id", "model_version") if k in run
                        },
                        "provenance_artifact": "provenance.json",
                        "engine": provenance.get("engine", {}),
                        "collection_specification": [d["specification"] for d in run["collection"]],
                    },
                    run["status"] == "succeeded",
                )
                run["datasets"] = [
                    {k: d[k] for k in ("id", "rows", "complete")} for d in summary["datasets"]
                ]
            solver = run.pop("solver_results", None)
            if solver is None and (directory / "solvers.json").exists():
                solver = json.loads((directory / "solvers.json").read_text())
            if solver is not None:
                from .solvers import enrich

                engine = json.loads((directory / "provenance.json").read_text()).get("engine", {})
                solver = enrich(solver, directory, engine.get("version", ""))
                (directory / "solvers.json").write_text(
                    json.dumps(solver, indent=2), encoding="utf-8"
                )
        except Exception as exc:
            run["results_error"] = {"code": "RESULT_PROCESSING_ERROR", "message": str(exc)}
        run["results_finalized"] = True

    def list_datasets(self, run_id):
        return {
            "run_id": run_id,
            "datasets": results.list_datasets(Path(self.get_run(run_id)["directory"])),
        }

    def query_results(self, run_id, dataset_id, **kwargs):
        return {
            "run_id": run_id,
            **results.query(Path(self.get_run(run_id)["directory"]), dataset_id, **kwargs),
        }

    def _artifact(self, run_id, path):
        root = Path(self.get_run(run_id)["directory"])
        relative = Path(path).relative_to(root).as_posix()
        identifier = hashlib.sha256(relative.encode()).hexdigest()[:32]
        return {
            "id": identifier,
            "path": relative,
            "uri": f"gmat://runs/{run_id}/artifacts/{identifier}",
        }

    def export_results(self, run_id, dataset_id):
        path = results.export_csv(Path(self.get_run(run_id)["directory"]), dataset_id)
        return {"run_id": run_id, "artifact": self._artifact(run_id, path)}

    async def plot_results(self, run_id, dataset_id, kind="time_history", columns=None):
        async with self._plot_lock:
            data = await asyncio.to_thread(
                results.plot, Path(self.get_run(run_id)["directory"]), dataset_id, kind, columns
            )
        return {
            "run_id": run_id,
            **data,
            "artifacts": [self._artifact(run_id, data[k]) for k in ("png", "svg", "preview")],
        }

    def get_solver_results(self, run_id):
        path = Path(self.get_run(run_id)["directory"]) / "solvers.json"
        return {
            "run_id": run_id,
            "solvers": json.loads(path.read_text()) if path.exists() else [],
            "available": path.exists(),
        }

    def list_runs(self, offset=0, limit=100):
        if offset < 0 or not 1 <= limit <= 1000:
            raise GmatError("INVALID_RANGE", "Invalid page")
        for run in self.catalog.entries("runs"):
            if run["run_id"] not in self.tasks:
                self.runs[run["run_id"]] = run
        runs = sorted(self.runs.values(), key=lambda r: r.get("created_at", ""), reverse=True)
        selected = [
            {
                k: v
                for k, v in run.items()
                if k
                in (
                    "run_id",
                    "session_id",
                    "profile_id",
                    "status",
                    "native_status",
                    "model_version",
                    "created_at",
                    "finished_at",
                    "directory",
                    "results_error",
                )
            }
            for run in runs[offset : offset + limit]
        ]
        return {"runs": selected, "total": len(runs)}

    async def save_project(self, session_id, name, inputs=None):
        session = self.session(session_id)
        self.available(session)
        async with session.lock:
            snapshot = await self._snapshot(session)
            identifier = uuid.uuid4().hex
            root = self.config.server.data_dir / "projects" / identifier
            root.mkdir(parents=True)
            (root / "mission.script").write_text(snapshot["text"], encoding="utf-8")
            copied = []
            for value in inputs or []:
                relative = Path(value)
                if relative.is_absolute() or ".." in relative.parts:
                    raise GmatError(
                        "INVALID_ARGUMENT",
                        "Project inputs must be relative to the project directory",
                    )
                source = Path(snapshot["project_dir"]) / relative
                if not source.is_file():
                    raise GmatError("MISSING_INPUT", str(source))
                destination = root / relative
                if destination.name in ("mission.script", "manifest.json"):
                    raise GmatError("INVALID_ARGUMENT", "Reserved project filename")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                copied.append(
                    {
                        "path": relative.as_posix(),
                        "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                    }
                )
            manifest = {
                "schema_version": 1,
                "project_id": identifier,
                "name": name,
                "profile_id": session.profile_id,
                "engine": session.worker.info,
                "datasets": session.datasets,
                "inputs": copied,
                "directory": str(root),
                "model_version": session.model_version,
                "profile": session.profile.model_dump(mode="json"),
                "startup_sha256": hashlib.sha256(session.worker.startup.read_bytes()).hexdigest(),
                "mission_sha256": hashlib.sha256(
                    (root / "mission.script").read_bytes()
                ).hexdigest(),
                "dependencies_policy": "Only explicitly listed inputs are bundled; installed GMAT data remain profile-owned",
            }
            (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            self.catalog.record("projects", identifier, manifest)
            return manifest

    def list_projects(self):
        return {"projects": self.catalog.entries("projects")}

    async def open_project(self, project_id, profile_id=None):
        project = next(
            (p for p in self.catalog.entries("projects") if p["project_id"] == project_id), None
        )
        if project is None:
            raise GmatError("UNKNOWN_PROJECT", "Project not found")
        root = Path(project["directory"])
        if (
            project.get("mission_sha256")
            and hashlib.sha256((root / "mission.script").read_bytes()).hexdigest()
            != project["mission_sha256"]
        ):
            raise GmatError("PROJECT_INPUT_CHANGED", "Saved mission checksum changed")
        for item in project["inputs"]:
            path = root / item["path"]
            if (
                not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]
            ):
                raise GmatError("PROJECT_INPUT_CHANGED", str(path))
        session = await self.create_session(profile_id or project["profile_id"])
        try:
            await self.load_script(
                session["session_id"], path=str(root / "mission.script"), project_dir=str(root)
            )
            opened = self.session(session["session_id"])
            opened.datasets = project["datasets"]
            return {
                **opened.describe(),
                "warnings": ["Selected engine differs from the saved project"]
                if opened.worker.info.get("native_libraries")
                != project["engine"].get("native_libraries")
                else [],
            }
        except BaseException:
            await self.close_session(session["session_id"])
            raise

    async def stop_studies(self):
        for task in list(self.study_tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self.study_tasks.values()), return_exceptions=True)
