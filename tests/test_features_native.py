from __future__ import annotations

from pathlib import Path

import pytest

from gmat_mcp.core import Manager

from .protocol_support import call, connected, wait_run, write_config
from .test_native import direct

pytestmark = pytest.mark.native
ROOT = Path(__file__).resolve().parents[1]
STATE = ["X", "Y", "Z", "VX", "VY", "VZ"]
SPEC = {
    "datasets": [
        {
            "id": "orbit",
            "spacecraft": "Sat",
            "frame": "EarthMJ2000Eq",
            "body": "Earth",
            "groundtrack": True,
        }
    ]
}


@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_new_tools_real_engines_and_transports(native_config, tmp_path, transport):
    config = native_config.model_copy(deep=True)
    config.server.data_dir = tmp_path / "data"
    config_path = tmp_path / "profiles.toml"
    write_config(config, config_path)
    async with connected(transport, config_path, tmp_path) as client:
        for profile_id, profile in config.profiles.items():
            sid = (await call(client, "create_session", profile_id=profile_id))["session_id"]
            script = ROOT / "examples/propagation.script"
            baseline = await direct(profile, script, tmp_path / f"baseline-{profile_id}")
            await call(client, "load_script", session_id=sid, path=str(script))
            configured = await call(client, "configure_results", session_id=sid, spec=SPEC)
            run = await wait_run(client, await call(client, "run_mission", session_id=sid))
            assert run["status"] == "succeeded", run
            state = await call(
                client, "get_object", session_id=sid, name="Sat", fields=STATE, view="runtime"
            )
            assert [f["value"] for f in state["fields"]] == pytest.approx(
                baseline["state"], rel=1e-12, abs=1e-9
            )
            meta = (await call(client, "list_datasets", run_id=run["run_id"]))["datasets"][0]
            assert meta["complete"] and meta["rows"] >= 3
            assert meta["model_version"] == configured["model_version"]
            series = await call(
                client,
                "query_results",
                run_id=run["run_id"],
                dataset_id="orbit",
                columns=["Sat.EarthMJ2000Eq.X", "Sat.EarthMJ2000Eq.Y"],
                limit=1000,
            )
            assert series["rows"][-1]["values"] == pytest.approx(
                baseline["state"][:2], rel=1e-12, abs=1e-9
            )
            exported = await call(
                client, "export_results", run_id=run["run_id"], dataset_id="orbit"
            )
            assert exported["artifact"]["path"].endswith(".csv")
            plotted = await client.call_tool(
                "plot_results",
                {"run_id": run["run_id"], "dataset_id": "orbit", "kind": "ground_track"},
            )
            assert not plotted.is_error, plotted
            assert any(c.type == "image" for c in plotted.content)
            assert plotted.structured_content["artifacts"]
            session = await call(client, "get_session", session_id=sid)
            dry = await call(
                client,
                "edit_mission",
                session_id=sid,
                expected_version=session["model_version"],
                changes=[{"op": "set_field", "name": "Sat", "field": "DryMass", "value": 333}],
                dry_run=True,
            )
            assert not dry["committed"]
            changed = await call(
                client,
                "edit_mission",
                session_id=sid,
                expected_version=session["model_version"],
                changes=[{"op": "set_field", "name": "Sat", "field": "DryMass", "value": 333}],
            )
            assert changed["model_version"] != session["model_version"]
            await call(
                client, "undo_edit", session_id=sid, expected_version=changed["model_version"]
            )
            assert (await call(client, "list_datasets", run_id=run["run_id"]))["datasets"][0][
                "rows"
            ] == meta["rows"]
            project = await call(client, "save_project", session_id=sid, name="native-project")
            assert project["project_id"]
            script_export = await call(client, "export_script", session_id=sid)
            await call(client, "load_script", session_id=sid, text=script_export["text"])
            round_trip = await wait_run(client, await call(client, "run_mission", session_id=sid))
            assert (await call(client, "list_datasets", run_id=round_trip["run_id"]))["datasets"][
                0
            ]["complete"]
            await call(
                client, "load_script", session_id=sid, path=str(ROOT / "examples/targeting.script")
            )
            target = await wait_run(client, await call(client, "run_mission", session_id=sid))
            solvers = (await call(client, "get_solver_results", run_id=target["run_id"]))["solvers"]
            assert solvers[0]["convergence"] == "converged", solvers
            assert solvers[0]["iterations"] == 3 and abs(solvers[0]["goals"][0]["residual"]) < 0.01
            await call(client, "close_session", session_id=sid)


@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_geo_collection_unchanged_and_native_results(native_config, tmp_path, transport):
    config = native_config.model_copy(deep=True)
    config.server.data_dir = tmp_path / "data"
    path = tmp_path / "profiles.toml"
    write_config(config, path)
    text = (
        (ROOT / "missions/AmericasGEO_15deg_90W_headless.script")
        .read_text()
        .replace("AmericasGEO", "Sat")
    )
    async with connected(transport, path, tmp_path) as client:
        for profile_id, profile in config.profiles.items():
            original = tmp_path / f"{profile_id}.script"
            original.write_text(text)
            baseline = await direct(profile, original, tmp_path / f"geo-direct-{profile_id}")
            sid = (await call(client, "create_session", profile_id=profile_id))["session_id"]
            await call(client, "load_script", session_id=sid, text=text)
            await call(client, "configure_results", session_id=sid, spec=SPEC)
            run = await wait_run(client, await call(client, "run_mission", session_id=sid))
            assert run["status"] == "succeeded", run
            state = await call(
                client, "get_object", session_id=sid, name="Sat", fields=STATE, view="runtime"
            )
            assert [f["value"] for f in state["fields"]] == pytest.approx(
                baseline["state"], rel=1e-12, abs=1e-9
            )
            page = await call(
                client,
                "query_results",
                run_id=run["run_id"],
                dataset_id="orbit",
                columns=["Sat.Earth.Longitude", "Sat.Earth.Latitude"],
                limit=1000,
            )
            records = list(page["rows"])
            while page["has_more"]:
                page = await call(
                    client,
                    "query_results",
                    run_id=run["run_id"],
                    dataset_id="orbit",
                    columns=["Sat.Earth.Longitude", "Sat.Earth.Latitude"],
                    offset=page["next_offset"],
                    limit=1000,
                )
                records.extend(page["rows"])
            assert len(records) >= 865
            longitude = [r["values"][0] for r in records]
            assert -94 < min(longitude) < -92 and -88 < max(longitude) < -86
            for kind in ("ground_track", "orbit_3d", "time_history"):
                chart = await client.call_tool(
                    "plot_results", {"run_id": run["run_id"], "dataset_id": "orbit", "kind": kind}
                )
                assert not chart.is_error, chart
            await call(client, "export_results", run_id=run["run_id"], dataset_id="orbit")
            await call(client, "close_session", session_id=sid)


async def test_native_solver_negative_and_studies(native_config, tmp_path):
    config = native_config.model_copy(deep=True)
    config.server.data_dir = tmp_path / "data"
    manager = Manager(config)
    try:
        for profile in config.profiles:
            sid = (await manager.create_session(profile))["session_id"]
            text = (
                (ROOT / "examples/targeting.script")
                .read_text()
                .replace("MaximumIterations = 30", "MaximumIterations = 1")
            )
            await manager.load_script(sid, text=text)
            run = await manager.run_mission(sid)
            await manager.tasks[run["run_id"]]
            solver = manager.get_solver_results(run["run_id"])["solvers"][0]
            assert solver["convergence"] == "not_converged", solver
            await manager.load_script(sid, path=str(ROOT / "examples/propagation.script"))
            study = await manager.start_study(
                sid,
                {
                    "mode": "optimize",
                    "profiles": [profile],
                    "variables": [{"field": "Sat.DryMass", "lower": 100, "upper": 300}],
                    "metrics": [{"name": "mass", "object": "Sat", "field": "DryMass"}],
                    "objective": {"metric": "mass", "direction": "minimize"},
                    "max_evaluations": 5,
                },
            )
            await manager.study_tasks[study["study_id"]]
            done = manager.get_study(study["study_id"])
            assert done["evaluations"] == 5 and done["stop_reason"] == "evaluation_budget", done
            assert done["best"]["objective"] < 200
            assert Path(done["best_script"]).is_file()
            assert all(c["feasible"] for c in manager.list_study_cases(study["study_id"])["cases"])
            await manager.close_session(sid)
    finally:
        await manager.shutdown()
    restarted = Manager(config)
    assert len(restarted.list_runs()["runs"]) >= 12
    assert restarted.studies
    await restarted.shutdown()
