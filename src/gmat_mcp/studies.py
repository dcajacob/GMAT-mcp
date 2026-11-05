"""Bounded mission evaluations. No user expressions are executed."""

from __future__ import annotations

import asyncio
import copy
import csv
import hashlib
import itertools
import json
import math
import shutil
import threading
import uuid
from pathlib import Path

from .errors import GmatError
from .models import StudySpec
from .results import connect, metadata


class StudyLimit(Exception):
    pass


def aggregate(values, spec):
    if any(v is None or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        raise GmatError("METRIC_UNAVAILABLE", "Metric includes unavailable or non-finite samples")
    data = [float(v) for v in values]
    if not data:
        raise GmatError("METRIC_UNAVAILABLE", "Metric has no finite values")
    transform = spec.get("transform", "identity")
    ref = spec.get("reference", 0)
    if transform == "difference":
        data = [v - ref for v in data]
    elif transform == "abs_difference":
        data = [abs(v - ref) for v in data]
    elif transform == "angular_distance":
        data = [abs((v - ref + 180) % 360 - 180) for v in data]
    elif transform != "identity":
        raise GmatError("INVALID_ARGUMENT", "Unknown metric transform")
    reduction = spec.get("reduce", "final")
    if reduction == "final":
        return data[-1]
    if reduction == "minimum":
        return min(data)
    if reduction == "maximum":
        return max(data)
    if reduction == "mean":
        return sum(data) / len(data)
    raise GmatError("INVALID_ARGUMENT", "Unknown metric reduction")


class Studies:
    def _save_study(self, study):
        self.catalog.record("studies", study["study_id"], study)
        (Path(study["directory"]) / "study.json").write_text(
            json.dumps(study, indent=2, allow_nan=False), encoding="utf-8"
        )

    async def start_study(self, session_id, spec):
        try:
            spec = StudySpec.model_validate(spec).model_dump(exclude_none=True)
        except ValueError as exc:
            raise GmatError("INVALID_ARGUMENT", str(exc)) from exc
        session = self.session(session_id)
        self.available(session)
        mode = spec.get("mode", "grid")
        profiles = spec.get("profiles", [session.profile_id])
        if (
            mode not in ("cases", "grid", "optimize")
            or not profiles
            or any(p not in self.config.profiles for p in profiles)
        ):
            raise GmatError("INVALID_ARGUMENT", "Invalid study mode or profiles")
        budget = spec.get("max_evaluations", 100)
        deadline = spec.get("timeout", 3600)
        if (
            not isinstance(budget, int)
            or not 1 <= budget <= 10000
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
            or not 0 < deadline <= 86400
        ):
            raise GmatError(
                "INVALID_ARGUMENT", "Study budget must be 1..10000; timeout 0..86400 seconds"
            )
        if mode == "optimize" and (
            len(profiles) != 1 or not spec.get("variables") or not spec.get("objective")
        ):
            raise GmatError(
                "INVALID_ARGUMENT",
                "Optimization needs one profile, bounded variables and an objective metric",
            )
        if not spec.get("metrics") or len({m["name"] for m in spec["metrics"]}) != len(
            spec["metrics"]
        ):
            raise GmatError("INVALID_ARGUMENT", "Supply unique named metrics")
        metric_names = {m["name"] for m in spec["metrics"]}
        if len(metric_names) > 64 or len(spec.get("variables", [])) > 20:
            raise GmatError("INVALID_ARGUMENT", "At most 64 metrics and 20 optimization variables")
        for metric in spec["metrics"]:
            fields = (
                ("object", "field") if metric["source"] == "runtime" else ("dataset_id", "column")
            )
            if not all(metric.get(k) for k in fields):
                raise GmatError("INVALID_ARGUMENT", f"Metric {metric['name']} requires {fields}")
        if spec.get("objective") and spec["objective"]["metric"] not in metric_names:
            raise GmatError("INVALID_ARGUMENT", "Objective refers to an unknown metric")
        for c in spec["constraints"]:
            if c["metric"] not in metric_names or c["tolerance"] < 0:
                raise GmatError("INVALID_ARGUMENT", "Invalid constraint metric or tolerance")
        async with session.lock:
            baseline = await self._snapshot(session)
            identifier = uuid.uuid4().hex
            root = self.config.server.data_dir / "studies" / identifier
            root.mkdir(parents=True)
            (root / "baseline.script").write_text(baseline["text"], encoding="utf-8")
            for value in spec.get("inputs", []):
                relative = Path(value)
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or relative.name in ("baseline.script", "study.json")
                ):
                    raise GmatError("INVALID_ARGUMENT", "Invalid study input path")
                source = Path(baseline["project_dir"]) / relative
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            study = {
                "study_id": identifier,
                "session_id": session_id,
                "model_version": session.model_version,
                "status": "queued",
                "mode": mode,
                "profiles": profiles,
                "spec": spec,
                "collection": baseline["datasets"],
                "directory": str(root),
                "cases": [],
                "evaluations": 0,
                "max_evaluations": budget,
                "timeout": deadline,
                "seed": spec.get("seed", 0),
                "optimizer_converged": False,
                "baseline_engine": session.worker.info,
                "server_instance": self.instance_id,
            }
            self.studies[identifier] = study
            self._save_study(study)
            self.study_tasks[identifier] = asyncio.create_task(self._execute_study(study))
            return self.get_study(identifier)

    def get_study(self, study_id):
        if study_id not in self.studies:
            raise GmatError("UNKNOWN_STUDY", "Study not found")
        return {k: v for k, v in self.studies[study_id].items() if k != "cases"}

    def list_study_cases(self, study_id, offset=0, limit=100):
        self.get_study(study_id)
        if offset < 0 or not 1 <= limit <= 1000:
            raise GmatError("INVALID_RANGE", "Invalid case page")
        cases = self.studies[study_id]["cases"]
        return {"study_id": study_id, "cases": cases[offset : offset + limit], "total": len(cases)}

    async def cancel_study(self, study_id):
        self.get_study(study_id)
        task = self.study_tasks.get(study_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if self.studies[study_id]["status"] == "queued":
                self.studies[study_id].update(status="cancelled", stop_reason="cancelled")
                self._save_study(self.studies[study_id])
                self.study_tasks.pop(study_id, None)
        return self.get_study(study_id)

    async def _case(self, study, params, profile):
        if study["evaluations"] >= study["max_evaluations"]:
            raise StudyLimit("evaluation_budget")
        study["evaluations"] += 1
        case = {
            "case_id": uuid.uuid4().hex,
            "parameters": params,
            "profile_id": profile,
            "status": "queued",
            "metrics": {},
            "feasible": False,
        }
        study["cases"].append(case)
        sid = None
        try:
            async with self._study_slots:
                while True:
                    if self.closing:
                        raise asyncio.CancelledError()
                    try:
                        info = await self.create_session(profile)
                        sid = info["session_id"]
                        break
                    except GmatError as exc:
                        if exc.code != "SESSION_LIMIT":
                            raise
                        await asyncio.sleep(0.1)
                case["status"] = "running"
                root = Path(study["directory"])
                session = self.session(sid)
                # This engine belongs only to this case and has no previous
                # model to preserve. Load directly instead of launching a second
                # temporary engine for an otherwise-empty case session.
                loaded = await session.worker.call(
                    "load_script", path=str(root / "baseline.script"), project_dir=str(root)
                )
                session.project_dir = root
                session.revision = loaded["revision"]
                session.datasets = copy.deepcopy(study["collection"])
                if session.datasets:
                    cols = [c for d in session.datasets for c in d["columns"]]
                    native = await session.worker.call(
                        "parameter_metadata", expressions=[c["expression"] for c in cols]
                    )
                    for col, meta in zip(cols, native["parameters"]):
                        col.update(meta)
                for key, value in params.items():
                    if "." not in key:
                        raise GmatError("INVALID_ARGUMENT", "Case parameters use Object.Field keys")
                    name, field = key.split(".", 1)
                    await self.call(
                        sid, "set_field", mutates=True, name=name, field=field, value=value
                    )
                run = await self.run_mission(sid)
                case["run_id"] = run["run_id"]
                task = self.tasks.get(run["run_id"])
                if task:
                    await asyncio.shield(task)
                run = self.get_run(run["run_id"])
                case["status"] = run["status"]
                case["native_status"] = run.get("native_status")
                if run.get("error"):
                    case["error"] = run["error"]
                if run["status"] == "succeeded":
                    for metric in study["spec"]["metrics"]:
                        if metric.get("source", "runtime") == "runtime":
                            obj = await self.call(
                                sid,
                                "get_object",
                                name=metric["object"],
                                fields=[metric["field"]],
                                view="runtime",
                            )
                            field = obj["fields"][0]
                            values = [field["value"]]
                            unit = field.get("unit")
                        elif metric["source"] == "dataset":
                            folder = Path(run["directory"])
                            meta = metadata(folder, metric["dataset_id"])
                            if not meta["complete"]:
                                raise GmatError("METRIC_UNAVAILABLE", "Dataset is incomplete")
                            columns = [c["expression"] for c in meta["columns"]]
                            index = columns.index(metric["column"])
                            with connect(folder) as db:
                                values = [
                                    json.loads(r[0])[index]
                                    for r in db.execute(
                                        "SELECT values_json FROM samples WHERE dataset=? ORDER BY seq",
                                        (metric["dataset_id"],),
                                    )
                                ]
                            unit = meta["columns"][index].get("unit")
                        else:
                            raise GmatError(
                                "INVALID_ARGUMENT", "Metric source must be runtime or dataset"
                            )
                        case["metrics"][metric["name"]] = {
                            "value": aggregate(values, metric),
                            "unit": unit,
                        }
                    solvers = self.get_solver_results(run["run_id"])["solvers"]
                    case["solvers"] = solvers
                    case["feasible"] = all(
                        s["convergence"] == "converged"
                        or (
                            s["convergence"] == "unavailable"
                            and study["spec"].get("accept_unknown_convergence", False)
                        )
                        for s in solvers
                    )
                    for c in study["spec"].get("constraints", []):
                        value = case["metrics"][c["metric"]]["value"]
                        target = c["value"]
                        tol = c.get("tolerance", 0)
                        ok = {
                            "<=": value <= target + tol,
                            ">=": value >= target - tol,
                            "==": abs(value - target) <= tol,
                        }
                        if c["op"] not in ok:
                            raise GmatError(
                                "INVALID_ARGUMENT", "Constraint op must be <=, >= or =="
                            )
                        case["feasible"] &= ok[c["op"]]
                    objective = study["spec"].get("objective")
                    if objective:
                        score = case["metrics"][objective["metric"]]["value"]
                        case["objective"] = score
                        best = study.get("best")
                        if case["feasible"] and (
                            best is None
                            or (
                                score < best["objective"]
                                if objective.get("direction", "minimize") == "minimize"
                                else score > best["objective"]
                            )
                        ):
                            study["best"] = {
                                k: case[k] for k in ("case_id", "run_id", "parameters", "objective")
                            }
                            target = root / "best.script"
                            shutil.copyfile(Path(run["directory"]) / "mission.script", target)
                            study["best_script"] = str(target)
        except asyncio.CancelledError:
            case["status"] = "cancelled"
            raise
        except Exception as exc:
            case.update(
                status="failed",
                error=exc.as_dict()
                if isinstance(exc, GmatError)
                else {"code": "CASE_ERROR", "message": str(exc)},
                feasible=False,
            )
        finally:
            if sid:
                await self.close_session(sid)
            self._save_study(study)
        return case

    async def _execute_study(self, study):
        study["status"] = "running"
        self._save_study(study)
        pending = set()
        try:
            async with asyncio.timeout(study["timeout"]):
                spec = study["spec"]
                if study["mode"] == "optimize":
                    await self._optimize(study)
                else:
                    if study["mode"] == "cases":
                        cases = spec.get("cases", [])
                    else:
                        grid = spec.get("grid", {})
                        keys = list(grid)
                        cases = (
                            dict(zip(keys, values))
                            for values in itertools.product(*(grid[k] for k in keys))
                        )
                    for params, profile in (
                        (case, profile) for case in cases for profile in study["profiles"]
                    ):
                        if study["evaluations"] + len(pending) >= study["max_evaluations"]:
                            if pending:
                                await asyncio.gather(*pending)
                                pending.clear()
                            if study["evaluations"] >= study["max_evaluations"]:
                                raise StudyLimit("evaluation_budget")
                        pending.add(asyncio.create_task(self._case(study, params, profile)))
                        if len(pending) >= min(4, self.config.server.max_sessions):
                            await asyncio.gather(*pending)
                            pending.clear()
                    if pending:
                        await asyncio.gather(*pending)
                    study["stop_reason"] = "completed"
                study["status"] = "succeeded"
        except StudyLimit as exc:
            study.update(status="succeeded", stop_reason=str(exc), optimizer_converged=False)
        except TimeoutError:
            study.update(status="timed_out", stop_reason="study_deadline")
        except asyncio.CancelledError:
            study.update(status="cancelled", stop_reason="cancelled")
        except Exception as exc:
            study.update(status="failed", stop_reason="error", error=str(exc))
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            self._save_study(study)
            self.study_tasks.pop(study["study_id"], None)

    async def _optimize(self, study):
        import numpy as np
        from scipy.optimize import NonlinearConstraint, differential_evolution

        variables = study["spec"]["variables"]
        for v in variables:
            if (
                not math.isfinite(v["lower"])
                or not math.isfinite(v["upper"])
                or v["lower"] >= v["upper"]
            ):
                raise GmatError(
                    "INVALID_ARGUMENT", "Optimization requires finite increasing bounds"
                )
        loop = asyncio.get_running_loop()
        stop = threading.Event()
        cache = {}
        active = set()

        async def population(matrix, constraints=False):
            points = np.atleast_2d(matrix)
            if matrix.ndim == 1:
                points = matrix[:, None]
            answers = []
            tasks = []
            keys = []
            for x in points.T:
                key = tuple(float(n) for n in x)
                keys.append(key)
                if key not in cache:
                    if study["evaluations"] + len(tasks) >= study["max_evaluations"]:
                        raise StudyLimit("evaluation_budget")
                    params = {
                        v["field"]: int(round(n)) if v.get("integer") else float(n)
                        for v, n in zip(variables, x)
                    }
                    task = asyncio.create_task(self._case(study, params, study["profiles"][0]))
                    active.add(task)
                    tasks.append((key, task))
                    cache[key] = None
            for key, task in tasks:
                cache[key] = await task
                active.discard(task)
            for key in keys:
                case = cache[key]
                if constraints:
                    violations = []
                    for constraint in study["spec"].get("constraints", []):
                        if not case or constraint["metric"] not in case["metrics"]:
                            violations.append(math.inf)
                            continue
                        value = case["metrics"][constraint["metric"]]["value"]
                        target = constraint["value"]
                        tolerance = constraint.get("tolerance", 0)
                        violations.append(
                            (value - target - tolerance)
                            if constraint["op"] == "<="
                            else (target - value - tolerance)
                            if constraint["op"] == ">="
                            else abs(value - target) - tolerance
                        )
                    violations.append(0 if case and case["feasible"] else 1)
                    answers.append(violations)
                    continue
                value = case.get("objective", math.inf) if case else math.inf
                if study["spec"]["objective"].get(
                    "direction", "minimize"
                ) == "maximize" and math.isfinite(value):
                    value = -value
                answers.append(value)
            if constraints:
                return (
                    np.array(answers).T
                    if answers
                    else np.empty((len(study["spec"].get("constraints", [])) + 1, 0))
                )
            return np.array(answers) if matrix.ndim > 1 else answers[0]

        def objective(matrix):
            if stop.is_set():
                raise StudyLimit("cancelled")
            return asyncio.run_coroutine_threadsafe(population(matrix), loop).result()

        def run():
            def violations(matrix):
                if stop.is_set():
                    raise StudyLimit("cancelled")
                result = asyncio.run_coroutine_threadsafe(population(matrix, True), loop).result()
                return result[:, 0] if matrix.ndim == 1 else result

            return differential_evolution(
                objective,
                [(v["lower"], v["upper"]) for v in variables],
                rng=study["seed"],
                updating="deferred",
                polish=False,
                vectorized=True,
                popsize=5,
                integrality=[v.get("integer", False) for v in variables],
                maxiter=study["max_evaluations"],
                constraints=(NonlinearConstraint(violations, -np.inf, 0),),
            )

        try:
            result = await asyncio.to_thread(run)
            study.update(
                optimizer_converged=bool(result.success),
                stop_reason="optimizer_converged" if result.success else "optimizer_stopped",
                optimizer_message=str(result.message),
            )
        finally:
            stop.set()
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)

    def compare_runs(self, run_ids):
        if not 1 <= len(run_ids) <= 100:
            raise GmatError("INVALID_ARGUMENT", "Compare 1..100 runs")
        compared = [
            {
                "run": {
                    k: v
                    for k, v in self.get_run(r).items()
                    if k in ("run_id", "profile_id", "status", "model_version", "native_status")
                },
                "datasets": self.list_datasets(r)["datasets"],
                "solvers": self.get_solver_results(r)["solvers"],
            }
            for r in run_ids
        ]
        directory = Path(self.get_run(run_ids[0])["directory"]) / "comparisons"
        directory.mkdir(exist_ok=True)
        path = directory / (hashlib.sha256(json.dumps(run_ids).encode()).hexdigest()[:16] + ".csv")
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(
                [
                    "run_id",
                    "profile_id",
                    "dataset",
                    "expression",
                    "unit",
                    "frame",
                    "origin",
                    "minimum",
                    "maximum",
                    "mean",
                    "final",
                    "valid_samples",
                ]
            )
            for item in compared:
                for dataset in item["datasets"]:
                    for column in dataset["columns"]:
                        stat = dataset.get("statistics", {}).get(column["expression"], {})
                        writer.writerow(
                            [
                                item["run"]["run_id"],
                                item["run"]["profile_id"],
                                dataset["id"],
                                column["expression"],
                                column.get("unit"),
                                column.get("frame"),
                                column.get("origin"),
                                *[
                                    stat.get(k)
                                    for k in (
                                        "minimum",
                                        "maximum",
                                        "mean",
                                        "final",
                                        "valid_samples",
                                    )
                                ],
                            ]
                        )
        return {
            "runs": compared,
            "comparison_artifact": {"run_id": run_ids[0], **self._artifact(run_ids[0], path)},
        }
