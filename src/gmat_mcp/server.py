from __future__ import annotations

import base64
import inspect
import json
import secrets
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

from . import __version__
from .core import Manager
from .errors import GmatError
from .models import EditChange, ObjectInfo, ResultsSpec, RunInfo, SessionInfo, StudySpec


class LocalAuth:
    """Single-user loopback endpoint; authentication is independent of MCP sessions."""

    def __init__(self, app, token: str, port: int):
        self.app, self.token, self.port = app, token, port

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {key.decode().lower(): value.decode() for key, value in scope["headers"]}
        allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}
        origin = headers.get("origin")
        parsed = urlparse(origin) if origin else None
        valid_origin = not origin or (
            parsed.scheme in ("http", "https")
            and parsed.netloc in allowed_hosts
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
        )
        if headers.get("host") not in allowed_hosts or not valid_origin:
            status, body = 403, b"Invalid local Host or Origin"
        elif not secrets.compare_digest(headers.get("authorization", ""), f"Bearer {self.token}"):
            status, body = 401, b"Bearer token required"
        else:
            return await self.app(scope, receive, send)
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": body})


def create_server(manager: Manager) -> MCPServer:
    @asynccontextmanager
    async def lifespan(server):
        try:
            yield {}
        finally:
            with anyio.CancelScope(shield=True):
                await manager.shutdown()

    server = MCPServer("GMAT", version=__version__, lifespan=lifespan)

    def tool(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            try:
                result = function(*args, **kwargs)
                return await result if inspect.isawaitable(result) else result
            except GmatError as exc:
                raise ToolError(json.dumps(exc.as_dict())) from exc
            except (ValueError, KeyError, TypeError) as exc:
                raise ToolError(
                    json.dumps({"code": "INVALID_ARGUMENT", "message": str(exc)})
                ) from exc

        read_only = function.__name__ in {
            "list_profiles",
            "get_session",
            "list_classes",
            "get_help",
            "list_objects",
            "get_object",
            "list_commands",
            "get_run",
            "get_logs",
            "list_artifacts",
            "read_artifact",
            "list_datasets",
            "query_results",
            "get_solver_results",
            "list_projects",
            "list_runs",
            "get_study",
            "list_study_cases",
            "list_desktops",
        }
        return server.tool(
            structured_output=True,
            annotations=ToolAnnotations(
                read_only_hint=read_only,
            ),
        )(wrapped)

    @tool
    def list_profiles() -> dict[str, Any]:
        """List configured NASA/Qt installations and their available Python bindings."""
        return manager.list_profiles()

    @tool
    async def create_session(profile_id: str) -> SessionInfo:
        """Open an isolated GMAT engine. Select an explicit installation profile."""
        return SessionInfo.model_validate(await manager.create_session(profile_id))

    @tool
    async def get_session(session_id: str) -> SessionInfo:
        """Inspect engine identity, model revision and active run; works while running."""
        return SessionInfo.model_validate(await manager.inspect_session(session_id))

    @tool
    async def reset_session(session_id: str) -> SessionInfo:
        """Replace a session's worker with a clean engine, discarding unsaved edits."""
        return SessionInfo.model_validate(await manager.reset_session(session_id))

    @tool
    async def close_session(session_id: str) -> dict[str, Any]:
        """Terminate a session and any active run; retain saved run artifacts."""
        return await manager.close_session(session_id)

    @tool
    async def list_classes(session_id: str, type: str = "UnknownObject") -> dict[str, Any]:
        """Discover GMAT classes actually available from this engine and its plugins."""
        return await manager.call(session_id, "list_classes", type=type)

    @tool
    async def get_help(session_id: str, topic: str = "") -> dict[str, Any]:
        """Read the installed engine's help for a class, object or API topic."""
        return await manager.call(session_id, "get_help", topic=topic)

    @tool
    async def list_objects(session_id: str, type: str = "") -> dict[str, Any]:
        """List configured mission objects, optionally filtered by GMAT base type."""
        return await manager.call(session_id, "list_objects", type=type)

    @tool
    async def create_object(
        session_id: str, type: str, name: str, constructor_args: list[str] | None = None
    ) -> ObjectInfo:
        """Construct a named GMAT object. Up to four extra native constructor arguments."""
        return ObjectInfo.model_validate(
            await manager.call(
                session_id,
                "create_object",
                mutates=True,
                type=type,
                name=name,
                constructor_args=constructor_args,
            )
        )

    @tool
    async def get_object(
        session_id: str,
        name: str,
        fields: list[str] | None = None,
        view: Literal["configured", "runtime"] = "configured",
    ) -> ObjectInfo:
        """Read object fields, types, units and frame/epoch context. Runtime reads require a run."""
        return ObjectInfo.model_validate(
            await manager.call(session_id, "get_object", name=name, fields=fields, view=view)
        )

    @tool
    async def set_field(
        session_id: str,
        name: str,
        field: str,
        value: str | bool | int | float | list[str] | list[float],
    ) -> dict[str, Any]:
        """Set one configured object field. Edits invalidate runtime state; values use GMAT units."""
        return await manager.call(
            session_id, "set_field", mutates=True, name=name, field=field, value=value
        )

    @tool
    async def list_commands(session_id: str) -> dict[str, Any]:
        """Inspect the nested mission command sequence. IDs are scoped to the worker generation."""
        return await manager.call(session_id, "list_commands")

    @tool
    async def append_command(session_id: str, type: str, description: str = "") -> dict[str, Any]:
        """Append a GMAT command, e.g. Propagate with description 'Prop(Sat) {Sat.ElapsedSecs = 60}'.

        Branch commands must be closed with their matching End command. Use script import
        for deleting, reordering or replacing arbitrary command sequences.
        """
        return await manager.call(
            session_id, "append_command", mutates=True, type=type, description=description
        )

    @tool
    async def load_script(
        session_id: str,
        text: str | None = None,
        path: str | None = None,
        project_dir: str | None = None,
        expected_version: str | None = None,
    ) -> dict[str, Any]:
        """Parse text or a script file in a replacement engine. Failed imports preserve the old model.

        File inputs default to their script directory; text defaults to the session directory.
        Explicit project_dir controls relative input lookup. This does not execute the mission.
        """
        return await manager.load_script(
            session_id,
            text=text,
            path=path,
            project_dir=project_dir,
            expected_version=expected_version,
        )

    @tool
    async def export_script(
        session_id: str, path: str | None = None, overwrite: bool = False
    ) -> dict[str, Any]:
        """Export the current configured model as GMAT script, optionally saving to an explicit path."""
        return await manager.export_script(session_id, path, overwrite)

    @tool
    async def validate_mission(session_id: str) -> dict[str, Any]:
        """Initialize configured objects without running. Does not certify execution or convergence."""
        return await manager.call(session_id, "validate")

    @tool
    async def run_mission(session_id: str, timeout: float | None = None) -> RunInfo:
        """Start an asynchronous run and return its run_id. Poll get_run for results.

        A run owns the session until it finishes. Cancellation/timeout require reset or reload.
        Native success does not imply solver convergence; inspect summaries and reports.
        """
        return RunInfo.model_validate(await manager.run_mission(session_id, timeout))

    @tool
    def get_run(run_id: str) -> RunInfo:
        """Read run status, native status code, diagnostics and summary."""
        return RunInfo.model_validate(manager.get_run(run_id))

    @tool
    async def cancel_run(run_id: str) -> RunInfo:
        """Cancel a run by terminating its engine worker. Unsaved session state is lost."""
        return RunInfo.model_validate(await manager.cancel_run(run_id))

    @tool
    async def get_logs(session_id: str, offset: int = 0, limit: int = 16384) -> dict[str, Any]:
        """Read native stdout/stderr by byte offset, including while a mission is running (max 64 KiB)."""
        session = manager.session(session_id)
        if session.backend == "desktop":
            return await session.worker.rpc("logs", offset=offset, limit=limit)
        return manager.get_logs(session_id, offset, limit)

    @tool
    def list_artifacts(run_id: str) -> dict[str, Any]:
        """List run snapshots, provenance, logs, reports and data products (up to 1000 files)."""
        return manager.list_artifacts(run_id)

    @tool
    def read_artifact(
        run_id: str, artifact_id: str, offset: int = 0, limit: int = 16384
    ) -> dict[str, Any]:
        """Read a listed artifact by ID and byte offset (max 64 KiB); binary chunks use base64."""
        return manager.read_artifact(run_id, artifact_id, offset, limit)

    @tool
    async def configure_results(
        session_id: str, spec: ResultsSpec, expected_version: str | None = None
    ) -> dict[str, Any]:
        """Stage native ReportFile sampling. spec.datasets: id, spacecraft, frame, body,
        cartesian=true, keplerian=true, groundtrack=false, parameters=[].
        Frame is required for Cartesian/elements; body for elements/ground tracks.
        Sampling follows native published epochs; no fixed cadence or interpolation.
        """
        return await manager.configure_results(session_id, spec.model_dump(), expected_version)

    @tool
    def list_datasets(run_id: str) -> dict[str, Any]:
        """List immutable run datasets, column units, frames, epochs and quality."""
        return manager.list_datasets(run_id)

    @tool
    def query_results(
        run_id: str,
        dataset_id: str,
        columns: list[str] | None = None,
        start: float | None = None,
        end: float | None = None,
        offset: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Read at most 1000 samples/256 KiB. start/end use native GMAT A1ModJulian;
        offset is the original sample index, not an index into filtered results.
        """
        return manager.query_results(
            run_id, dataset_id, columns=columns, start=start, end=end, offset=offset, limit=limit
        )

    @tool
    def export_results(run_id: str, dataset_id: str) -> dict[str, Any]:
        """Export every collected sample as CSV, returned as a run artifact."""
        return manager.export_results(run_id, dataset_id)

    @tool
    async def plot_results(
        run_id: str,
        dataset_id: str,
        kind: Literal["time_history", "ground_track", "orbit_3d"] = "time_history",
        columns: list[str] | None = None,
    ) -> CallToolResult:
        """Generate PNG/SVG artifacts and an inline image. Time histories require
        columns sharing units; other plots select collected position/ground-track roles.
        """
        data = await manager.plot_results(run_id, dataset_id, kind, columns)
        preview = Path(data.pop("preview")).read_bytes()
        if len(preview) > 2 * 1024 * 1024:
            raise GmatError("PREVIEW_LIMIT", "Preview exceeded 2 MiB; read the plot artifacts")
        return CallToolResult(
            content=[
                TextContent(type="text", text=__import__("json").dumps(data)),
                ImageContent(
                    type="image", mime_type="image/png", data=base64.b64encode(preview).decode()
                ),
            ],
            structured_content=data,
        )

    @tool
    async def edit_mission(
        session_id: str,
        expected_version: str,
        changes: list[EditChange],
        dry_run: bool = False,
        validation: Literal["parse", "initialize"] = "parse",
    ) -> dict[str, Any]:
        """Atomic edits with a script diff. ops: create_object, set_field,
        append_command, insert_command, replace_command, remove_command, move_command.
        Command edits use command_id/text/position(before|after)/target_id.
        """
        return await manager.edit_mission(
            session_id,
            expected_version,
            [c.model_dump(exclude_none=True) for c in changes],
            dry_run,
            validation,
        )

    @tool
    async def undo_edit(session_id: str, expected_version: str) -> dict[str, Any]:
        """Restore the previous committed edit in a fresh engine."""
        return await manager.restore_edit(session_id, expected_version)

    @tool
    async def redo_edit(session_id: str, expected_version: str) -> dict[str, Any]:
        """Restore the next undone edit; creates a new model version."""
        return await manager.restore_edit(session_id, expected_version, redo=True)

    @tool
    def get_solver_results(run_id: str) -> dict[str, Any]:
        """Native solver status and version-qualified diagnostics, separately from execution."""
        return manager.get_solver_results(run_id)

    @tool
    async def save_project(
        session_id: str, name: str, inputs: list[str] | None = None
    ) -> dict[str, Any]:
        """Save mission and explicitly listed relative input files; installed data remain external."""
        return await manager.save_project(session_id, name, inputs)

    @tool
    def list_projects() -> dict[str, Any]:
        """List saved projects retained across server restarts."""
        return manager.list_projects()

    @tool
    async def open_project(project_id: str, profile_id: str | None = None) -> dict[str, Any]:
        """Open a saved project in a fresh session; optional explicit backend substitution."""
        return await manager.open_project(project_id, profile_id)

    @tool
    def list_runs(offset: int = 0, limit: int = 100) -> dict[str, Any]:
        """List retained runs, including historical manifests imported at startup."""
        return manager.list_runs(offset, limit)

    @tool
    async def start_study(session_id: str, spec: StudySpec) -> dict[str, Any]:
        """Freeze a baseline and start cases/grid/optimize. Parameter keys are Object.Field.
        spec: mode, profiles, cases:[{field:value}], grid:{field:[values]},
        variables:[{field,lower,upper,integer}], metrics:[{name,source:runtime|dataset,
        object,field,dataset_id,column,reduce:final|minimum|maximum|mean,
        transform:identity|difference|abs_difference|angular_distance,reference}],
        objective:{metric,direction:minimize|maximize}, constraints:[{metric,op,value,tolerance}],
        max_evaluations=100, timeout=3600, seed=0, inputs:[relative paths].
        """
        return await manager.start_study(session_id, spec.model_dump(exclude_none=True))

    @tool
    def get_study(study_id: str) -> dict[str, Any]:
        """Inspect asynchronous study status, budgets and best feasible case."""
        return manager.get_study(study_id)

    @tool
    async def cancel_study(study_id: str) -> dict[str, Any]:
        """Cancel queued/running cases; preserve case artifacts and the baseline."""
        return await manager.cancel_study(study_id)

    @tool
    def list_study_cases(study_id: str, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        """Read a bounded page of study parameters, outcomes and metrics."""
        return manager.list_study_cases(study_id, offset, limit)

    @tool
    def compare_runs(run_ids: list[str]) -> dict[str, Any]:
        """Compare run outcomes and available datasets; engine identities stay separate."""
        return manager.compare_runs(run_ids)

    @tool
    async def list_desktops() -> dict[str, Any]:
        """Discover opted-in local GMAT-Qt instances; authentication tokens are never returned."""
        return await manager.list_desktops()

    @tool
    async def attach_desktop(desktop_id: str, profile_id: str) -> SessionInfo:
        """Attach to one live GMAT-Qt document using its matching installation profile."""
        return SessionInfo.model_validate(await manager.attach_desktop(desktop_id, profile_id))

    @tool
    async def detach_desktop(session_id: str) -> dict[str, Any]:
        """Detach without closing GMAT-Qt. An MCP-owned active run receives a normal stop request."""
        if manager.session(session_id).backend != "desktop":
            raise GmatError("INVALID_ARGUMENT", "Session is not a desktop attachment")
        return await manager.close_session(session_id)

    @tool
    async def build_desktop(session_id: str, expected_version: str) -> SessionInfo:
        """Build the current GUI editor explicitly; reject stale versions and pending panel edits."""
        return SessionInfo.model_validate(await manager.build_desktop(session_id, expected_version))

    @tool
    async def pause_run(run_id: str) -> dict[str, Any]:
        """Cooperatively pause a desktop run at a native interruption checkpoint."""
        return await manager.control_desktop_run(run_id, "pause")

    @tool
    async def resume_run(run_id: str) -> dict[str, Any]:
        """Resume a paused desktop run."""
        return await manager.control_desktop_run(run_id, "resume")

    @server.resource("gmat://runs/{run_id}/datasets/{dataset_id}")
    def dataset_resource(run_id: str, dataset_id: str) -> str:
        from .results import metadata

        return json.dumps(metadata(Path(manager.get_run(run_id)["directory"]), dataset_id))

    @server.resource("gmat://profiles")
    def profiles_resource() -> str:
        return json.dumps(manager.list_profiles())

    @server.resource("gmat://sessions/{session_id}")
    def session_resource(session_id: str) -> str:
        return json.dumps(manager.session(session_id).describe())

    @server.resource("gmat://runs/{run_id}")
    def run_resource(run_id: str) -> str:
        return json.dumps(manager.get_run(run_id))

    @server.resource("gmat://runs/{run_id}/artifacts/{artifact_id}")
    def artifact_resource(run_id: str, artifact_id: str) -> str:
        return json.dumps(manager.read_artifact(run_id, artifact_id))

    return server
