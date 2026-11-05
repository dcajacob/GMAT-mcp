from __future__ import annotations

import json

import pytest

from gmat_mcp import results
from gmat_mcp.catalog import Catalog
from gmat_mcp.editing import edit_commands, statements
from gmat_mcp.errors import GmatError
from gmat_mcp.studies import aggregate


def test_native_dataset_quality_paging_export_and_plots(tmp_path):
    spec = {
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
    datasets = results.collection(spec)
    columns = datasets[0]["columns"]
    out = tmp_path / "output"
    out.mkdir()
    header = ",".join(c["expression"] for c in columns)
    rows = []
    for seq, t in enumerate((1, 2, 2, 1.5, 3)):
        row = [t, seq * 60] + [float(i + seq) for i in range(len(columns) - 2)]
        row[-3] = 179 if seq < 2 else -179
        rows.append(",".join(map(str, row)))
    rows[2] = rows[2].replace(",120,", ",NaN,")
    (out / "mcp_orbit.csv").write_text(header + "\n" + "\n".join(rows) + "\n" + header + "\n1,2\n")
    ingested = results.ingest(
        tmp_path, datasets, {"run_id": "run", "profile_id": "fake", "model_version": "v"}, True
    )["datasets"][0]
    assert ingested["rows"] == 5 and ingested["time_reversals"] == 1
    assert not ingested["complete"] and ingested["malformed_rows"] == 1
    first = results.query(tmp_path, "orbit", limit=2)
    second = results.query(tmp_path, "orbit", offset=first["next_offset"], limit=2)
    assert first["has_more"] and second["rows"][0]["invalid_columns"]
    assert results.query(tmp_path, "orbit", start=2, end=2)["rows"][1]["index"] == 2
    assert len(results.export_csv(tmp_path, "orbit").read_text().splitlines()) == 6
    for kind in ("ground_track", "orbit_3d", "time_history"):
        plotted = results.plot(tmp_path, "orbit", kind)
        assert __import__("pathlib").Path(plotted["png"]).stat().st_size > 1000
    with pytest.raises(GmatError):
        results.query(tmp_path, "orbit", limit=1001)
    with pytest.raises(GmatError):
        results.query(tmp_path, "orbit", columns=["not a column"])


def test_command_branch_mapping_and_batch():
    source = "Create Spacecraft Sat;\nBeginMissionSequence;\nTarget DC;\n Vary DC(Burn.Element1=1);\nEndTarget;\nPropagate P(Sat) {Sat.ElapsedSecs=60};\nReport R Sat.X;\n"
    tree = [
        {"id": "begin", "type": "BeginMissionSequence"},
        {
            "id": "target",
            "type": "Target",
            "children": [{"id": "vary", "type": "Vary"}, {"id": "end", "type": "EndTarget"}],
        },
        {"id": "prop", "type": "Propagate"},
        {"id": "report", "type": "Report"},
    ]
    removed = edit_commands(source, tree, [{"op": "remove_command", "command_id": "target"}])
    assert "Vary" not in removed and "EndTarget" not in removed and "Propagate" in removed
    moved = edit_commands(
        source,
        tree,
        [
            {
                "op": "move_command",
                "command_id": "target",
                "target_id": "report",
                "position": "after",
            }
        ],
    )
    assert moved.index("Report R") < moved.index("Target DC")
    batch = edit_commands(
        source,
        tree,
        [
            {
                "op": "replace_command",
                "command_id": "prop",
                "text": "Propagate P(Sat) {Sat.ElapsedSecs=120};",
            },
            {"op": "replace_command", "command_id": "report", "text": "Report R Sat.Y;"},
        ],
    )
    assert "120" in batch and "Sat.Y" in batch
    with pytest.raises(GmatError):
        edit_commands(
            source,
            tree,
            [
                {"op": "remove_command", "command_id": "target"},
                {"op": "remove_command", "command_id": "vary"},
            ],
        )
    assert len(statements("% comment\nGMAT A = 'a;''b'; % tail\nBeginMissionSequence;")) == 2


async def test_atomic_edits_history_failed_edit_and_persistence(manager, fake_config):
    sid = (await manager.create_session("fake"))["session_id"]
    await manager.load_script(sid, text="normal")
    original = manager.session(sid).model_version
    run = await manager.run_mission(sid)
    await manager.tasks[run["run_id"]]
    changes = [{"op": "set_field", "name": "Sat", "field": "DryMass", "value": 200}]
    preview = await manager.edit_mission(sid, original, changes, dry_run=True)
    assert not preview["committed"] and manager.session(sid).model_version == original
    assert manager.session(sid).runtime_run == run["run_id"]
    with pytest.raises(GmatError):
        await manager.edit_mission(
            sid,
            original,
            [*changes, {"op": "set_field", "name": "Sat", "field": "NotAField", "value": 0}],
        )
    assert manager.session(sid).model_version == original
    assert (await manager.call(sid, "get_object", name="Sat", fields=["DryMass"]))["fields"][0][
        "value"
    ] == 100
    committed = await manager.edit_mission(sid, original, changes)
    assert committed["committed"] and manager.session(sid).runtime_run is None
    with pytest.raises(GmatError):
        await manager.edit_mission(sid, original, changes)
    undone = await manager.restore_edit(sid, committed["model_version"])
    assert (await manager.call(sid, "get_object", name="Sat", fields=["DryMass"]))["fields"][0][
        "value"
    ] == 100
    await manager.restore_edit(sid, undone["model_version"], redo=True)
    assert (await manager.call(sid, "get_object", name="Sat", fields=["DryMass"]))["fields"][0][
        "value"
    ] == 200
    saved = await manager.save_project(sid, "saved")
    opened = await manager.open_project(saved["project_id"])
    assert opened["session_id"] != sid
    assert (await manager.call(opened["session_id"], "get_object", name="Sat", fields=["DryMass"]))[
        "fields"
    ][0]["value"] == 200
    catalog = Catalog(fake_config.server.data_dir)
    assert any(r["run_id"] == run["run_id"] for r in catalog.entries("runs"))
    assert catalog.entries("projects")[0]["name"] == "saved"
    catalog.close()


async def test_study_sweep_and_cancel_preserve_baseline(manager):
    sid = (await manager.create_session("fake"))["session_id"]
    await manager.load_script(sid, text="normal")
    version = manager.session(sid).model_version
    spec = {
        "mode": "grid",
        "grid": {"Sat.DryMass": [100, 200]},
        "metrics": [{"name": "mass", "object": "Sat", "field": "DryMass"}],
    }
    study = await manager.start_study(sid, spec)
    await manager.study_tasks[study["study_id"]]
    cases = manager.list_study_cases(study["study_id"])["cases"]
    assert sorted(c["metrics"]["mass"]["value"] for c in cases) == [100, 200]
    assert all(c["feasible"] for c in cases)
    assert manager.session(sid).model_version == version
    await manager.load_script(sid, text="slow")
    study = await manager.start_study(sid, spec)
    await __import__("asyncio").sleep(0.1)
    cancelled = await manager.cancel_study(study["study_id"])
    assert cancelled["status"] == "cancelled"
    assert len(manager.sessions) == 1


def test_metrics_no_expressions_and_angular_wrap():
    assert (
        aggregate(
            [-179, 179], {"transform": "angular_distance", "reference": 180, "reduce": "maximum"}
        )
        == 1
    )
    with pytest.raises(GmatError):
        aggregate([1], {"transform": "eval"})
    with pytest.raises(GmatError):
        aggregate([], {})


def test_catalog_interrupts_without_replaying(tmp_path):
    path = tmp_path / "sessions/s/runs/r"
    path.mkdir(parents=True)
    (path / "run.json").write_text(json.dumps({"run_id": "r", "status": "running"}))
    catalog = Catalog(tmp_path)
    assert catalog.entries("runs")[0]["error"]["code"] == "INTERRUPTED"
    catalog.close()


async def test_catalog_preserves_another_live_server(manager, fake_config, tmp_path):
    manager.catalog.record_run(
        {
            "run_id": "live",
            "status": "running",
            "server_instance": manager.instance_id,
            "directory": str(tmp_path),
        }
    )
    from gmat_mcp.core import Manager

    other = Manager(fake_config)
    try:
        assert other.get_run("live")["status"] == "running"
    finally:
        await other.shutdown()


async def test_optimizer_seed_budget_and_immediate_cancel(manager):
    sid = (await manager.create_session("fake"))["session_id"]
    await manager.load_script(sid, text="normal")
    spec = {
        "mode": "optimize",
        "variables": [{"field": "Sat.DryMass", "lower": 100, "upper": 300}],
        "metrics": [{"name": "mass", "object": "Sat", "field": "DryMass"}],
        "objective": {"metric": "mass"},
        "constraints": [{"metric": "mass", "op": ">=", "value": 150}],
        "max_evaluations": 10,
    }
    parameters = []
    for _ in range(2):
        study = await manager.start_study(sid, spec)
        await manager.study_tasks[study["study_id"]]
        completed = manager.get_study(study["study_id"])
        assert completed["status"] == "succeeded" and completed["evaluations"] <= 10, completed
        assert completed["best"]["objective"] >= 150
        parameters.append(
            [c["parameters"] for c in manager.list_study_cases(study["study_id"])["cases"]]
        )
    assert parameters[0] == parameters[1]
    study = await manager.start_study(sid, spec)
    assert (await manager.cancel_study(study["study_id"]))["status"] == "cancelled"
    assert len(manager.sessions) == 1


def test_large_result_pages_and_bounded_plot_samples(tmp_path):
    datasets = results.collection(
        {
            "datasets": [
                {"id": "large", "spacecraft": "Sat", "frame": "EarthMJ2000Eq", "body": "Earth"}
            ]
        }
    )
    cols = datasets[0]["columns"]
    path = tmp_path / "output"
    path.mkdir()
    with (path / "mcp_large.csv").open("w") as stream:
        stream.write(",".join(c["expression"] for c in cols) + "\n")
        for i in range(12000):
            stream.write(
                ",".join(str(float(i) + j * 0.3333333333333333) for j in range(len(cols))) + "\n"
            )
    results.ingest(tmp_path, datasets, {"run_id": "large"}, True)
    page = results.query(tmp_path, "large", limit=1000)
    assert len(json.dumps(page).encode()) <= 256 * 1024
    plotted = results.plot(tmp_path, "large")
    assert plotted["display_downsampled"] and plotted["samples"] == 12000
    assert results.metadata(tmp_path, "large")["statistics"]["Sat.ElapsedSecs"]["maximum"] > 11999
