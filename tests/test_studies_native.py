from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from .protocol_support import call, connected, write_config

pytestmark = pytest.mark.native
ROOT = Path(__file__).resolve().parents[1]


async def study_done(client, study):
    async with asyncio.timeout(180):
        while study["status"] in ("queued", "running"):
            await asyncio.sleep(0.1)
            study = await call(client, "get_study", study_id=study["study_id"])
    assert study["status"] == "succeeded", study
    return study


@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_geo_sweeps_and_constrained_maneuver_search(native_config, tmp_path, transport):
    config = native_config.model_copy(deep=True)
    config.server.data_dir = tmp_path / "data"
    path = tmp_path / "profiles.toml"
    write_config(config, path)
    profiles = list(config.profiles)
    geo = (
        (ROOT / "missions/AmericasGEO_15deg_90W_headless.script")
        .read_text()
        .replace("AmericasGEO", "Sat")
    )
    async with connected(transport, path, tmp_path) as client:
        sid = (await call(client, "create_session", profile_id=profiles[0]))["session_id"]
        await call(client, "load_script", session_id=sid, text=geo)
        await call(
            client,
            "configure_results",
            session_id=sid,
            spec={
                "datasets": [
                    {
                        "id": "geo",
                        "spacecraft": "Sat",
                        "frame": "EarthMJ2000Eq",
                        "body": "Earth",
                        "groundtrack": True,
                    }
                ]
            },
        )
        version = (await call(client, "get_session", session_id=sid))["model_version"]
        metrics = [
            {
                "name": "confinement",
                "source": "dataset",
                "dataset_id": "geo",
                "column": "Sat.Earth.Longitude",
                "transform": "angular_distance",
                "reference": -90,
                "reduce": "maximum",
            }
        ]
        sweep = await study_done(
            client,
            await call(
                client,
                "start_study",
                session_id=sid,
                spec={
                    "mode": "grid",
                    "profiles": profiles,
                    "grid": {"Sat.INC": [14, 16], "Sat.ECC": [0.02, 0.025]},
                    "metrics": metrics,
                },
            ),
        )
        cases = (await call(client, "list_study_cases", study_id=sweep["study_id"]))["cases"]
        assert len(cases) == 8 and all(c["feasible"] for c in cases), cases
        comparison = await call(client, "compare_runs", run_ids=[c["run_id"] for c in cases[:2]])
        assert comparison["comparison_artifact"]["path"].endswith(".csv")
        for profile in profiles:
            optimized = await study_done(
                client,
                await call(
                    client,
                    "start_study",
                    session_id=sid,
                    spec={
                        "mode": "optimize",
                        "profiles": [profile],
                        "variables": [
                            {
                                "field": "Sat.RAAN",
                                "lower": 218.9409657635839,
                                "upper": 226.9409657635839,
                            }
                        ],
                        "metrics": metrics,
                        "objective": {"metric": "confinement"},
                        "max_evaluations": 10,
                    },
                ),
            )
            assert optimized["evaluations"] <= 10
            assert optimized["best"]["objective"] < 4.5, optimized
        assert (await call(client, "get_session", session_id=sid))["model_version"] == version
        await call(
            client, "load_script", session_id=sid, path=str(ROOT / "examples/maneuver.script")
        )
        for profile in profiles:
            optimized = await study_done(
                client,
                await call(
                    client,
                    "start_study",
                    session_id=sid,
                    spec={
                        "mode": "optimize",
                        "profiles": [profile],
                        "variables": [{"field": "Burn.Element1", "lower": 0.1, "upper": 0.4}],
                        "metrics": [
                            {"name": "dv", "object": "Burn", "field": "Element1"},
                            {"name": "sma", "object": "Sat", "field": "SMA"},
                        ],
                        "objective": {"metric": "dv"},
                        "constraints": [{"metric": "sma", "op": ">=", "value": 7500}],
                        "max_evaluations": 15,
                    },
                ),
            )
            assert (
                optimized["evaluations"] <= 15 and 0.24 < optimized["best"]["objective"] < 0.35
            ), optimized
            candidates = (await call(client, "list_study_cases", study_id=optimized["study_id"]))[
                "cases"
            ]
            best = next(c for c in candidates if c["case_id"] == optimized["best"]["case_id"])
            assert best["feasible"] and best["metrics"]["sma"]["value"] >= 7500
        await call(client, "close_session", session_id=sid)
