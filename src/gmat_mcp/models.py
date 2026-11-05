"""Public result schemas. Dynamic GMAT values retain their engine-provided types."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, JsonValue


class Result(BaseModel):
    model_config = ConfigDict(extra="allow")


class EngineInfo(Result):
    python: str
    platform: str
    api: str
    version: str
    native_libraries: list[dict[str, str]]
    classes: list[str]
    capabilities: list[str]


class SessionInfo(Result):
    session_id: str
    profile_id: str
    state: Literal["ready", "lost"]
    revision: int
    generation: int
    active_run: str | None
    runtime_run: str | None
    engine: EngineInfo
    directory: str
    model_version: str = ""
    backend: str = "worker"


class DatasetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    spacecraft: str
    frame: str | None = None
    body: str | None = None
    cartesian: bool = True
    keplerian: bool = True
    groundtrack: bool = False
    parameters: list[str] = []


class ResultsSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    datasets: list[DatasetSpec] = []


class EditChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal[
        "create_object",
        "set_field",
        "append_command",
        "insert_command",
        "replace_command",
        "remove_command",
        "move_command",
    ]
    name: str | None = None
    type: str | None = None
    constructor_args: list[str] | None = None
    field: str | None = None
    value: JsonValue = None
    command_id: str | None = None
    target_id: str | None = None
    position: Literal["before", "after"] = "before"
    text: str | None = None


class MetricSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str
    source: Literal["runtime", "dataset"] = "runtime"
    object: str | None = None
    field: str | None = None
    dataset_id: str | None = None
    column: str | None = None
    reduce: Literal["final", "minimum", "maximum", "mean"] = "final"
    transform: Literal["identity", "difference", "abs_difference", "angular_distance"] = "identity"
    reference: float = 0


class StudyVariable(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    field: str
    lower: float
    upper: float
    integer: bool = False


class StudyObjective(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: str
    direction: Literal["minimize", "maximize"] = "minimize"


class StudyConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    metric: str
    op: Literal["<=", ">=", "=="]
    value: float
    tolerance: float = 0


class StudySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    mode: Literal["cases", "grid", "optimize"] = "grid"
    profiles: list[str] | None = None
    cases: list[dict[str, JsonValue]] = []
    grid: dict[str, list[JsonValue]] = {}
    variables: list[StudyVariable] = []
    metrics: list[MetricSpec]
    objective: StudyObjective | None = None
    constraints: list[StudyConstraint] = []
    max_evaluations: int = 100
    timeout: float = 3600
    seed: int = 0
    inputs: list[str] = []
    accept_unknown_convergence: bool = False


class FieldValue(Result):
    name: str
    type: str
    unit: str | None
    read_only: bool | None
    value: JsonValue
    text: str
    typed: bool


class FieldError(Result):
    name: str
    error: str


class ObjectInfo(Result):
    session_id: str
    generation: int
    name: str
    type: str
    view: Literal["configured", "runtime"]
    revision: int
    context: dict[str, str]
    fields: list[FieldValue | FieldError]
    run_id: str | None = None


class RunInfo(Result):
    run_id: str
    session_id: str
    profile_id: str
    status: Literal["queued", "running", "succeeded", "failed", "timed_out", "cancelled"]
    created_at: str
    revision: int
    generation: int
    directory: str
    started_at: str | None = None
    finished_at: str | None = None
    native_status: int | None = None
    summary: str | None = None
    error: dict[str, Any] | None = None
