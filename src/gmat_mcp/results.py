"""Native report collection, bounded dataset access and headless figures."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sqlite3
from pathlib import Path

from .errors import GmatError
from .models import ResultsSpec

MARKER = "% GMAT-MCP-Results "


def embedded_spec(text):
    markers = [line[len(MARKER) :] for line in text.splitlines() if line.startswith(MARKER)]
    if len(markers) > 1:
        raise GmatError("RESULTS_CONFIG_ERROR", "Ambiguous embedded results specification")
    if not markers:
        return None
    return ResultsSpec.model_validate(json.loads(markers[0])).model_dump()


def with_spec(text, spec):
    text = "\n".join(line for line in text.splitlines() if not line.startswith(MARKER)) + "\n"
    return MARKER + json.dumps(spec, separators=(",", ":")) + "\n" + text


def collection(spec: dict) -> list[dict]:
    parsed = ResultsSpec.model_validate(spec)
    datasets = []
    seen = set()
    for item in parsed.datasets:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", item.id) or item.id in seen:
            raise GmatError(
                "INVALID_ARGUMENT",
                "Dataset IDs must be unique GMAT identifiers (48 characters max)",
            )
        seen.add(item.id)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", item.spacecraft):
            raise GmatError("INVALID_ARGUMENT", "Invalid spacecraft name")
        if (item.cartesian or item.keplerian) and not item.frame:
            raise GmatError(
                "INVALID_ARGUMENT", "Cartesian/orbital collection requires an explicit frame"
            )
        if item.groundtrack and not item.body:
            raise GmatError("INVALID_ARGUMENT", "Ground track collection requires an explicit body")
        if item.keplerian and not item.body:
            raise GmatError(
                "INVALID_ARGUMENT",
                "Orbital elements require an explicit origin body as well as frame",
            )
        sc = item.spacecraft
        columns = [
            dict(
                expression=f"{sc}.A1ModJulian",
                unit="day",
                role="epoch",
                time_system="A1",
                epoch_format="GMATModJulian",
                jd_offset=2430000.0,
            ),
            dict(expression=f"{sc}.ElapsedSecs", unit="s", role="elapsed"),
        ]
        if item.cartesian:
            columns += [
                dict(
                    expression=f"{sc}.{item.frame}.{p}",
                    unit="km/s" if p.startswith("V") else "km",
                    frame=item.frame,
                    role=p,
                )
                for p in ("X", "Y", "Z", "VX", "VY", "VZ")
            ]
        if item.keplerian:
            columns += [
                dict(
                    expression=f"{sc}.{item.body if p in ('SMA', 'ECC', 'TA') else item.frame}.{p}",
                    unit={"SMA": "km", "ECC": "1"}.get(p, "deg"),
                    frame=item.frame if p in ("INC", "RAAN", "AOP") else None,
                    origin=item.body,
                    role=p,
                )
                for p in ("SMA", "ECC", "INC", "RAAN", "AOP", "TA")
            ]
        if item.groundtrack:
            columns += [
                dict(
                    expression=f"{sc}.{item.body}.{p}",
                    unit="km" if p == "Altitude" else "deg",
                    origin=item.body,
                    frame="body-fixed",
                    role=p,
                )
                for p in ("Longitude", "Latitude", "Altitude")
            ]
        columns += [dict(expression=p, unit=None, role="custom") for p in item.parameters]
        for col in columns:
            if len(col["expression"]) > 256 or not re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", col["expression"]
            ):
                raise GmatError(
                    "INVALID_ARGUMENT", "Only named scalar GMAT parameters can be collected"
                )
            col.update(
                type="Real",
                unit_source="GMAT parameter definition" if col["unit"] else "unavailable",
            )
        if len(columns) > 128 or len({c["expression"] for c in columns}) != len(columns):
            raise GmatError("INVALID_ARGUMENT", "Duplicate parameters or more than 128 columns")
        datasets.append(
            dict(
                id=item.id,
                report=f"MCPResults_{item.id}",
                filename=f"mcp_{item.id}.csv",
                columns=columns,
                specification=item.model_dump(),
            )
        )
    if len(datasets) > 32:
        raise GmatError("INVALID_ARGUMENT", "At most 32 datasets per mission")
    return datasets


def report_script(datasets: list[dict]) -> str:
    blocks = []
    for data in datasets:
        n = data["report"]
        params = ", ".join(c["expression"] for c in data["columns"])
        blocks.append(
            f"Create ReportFile {n};\nGMAT {n}.Filename = '{data['filename']}';\nGMAT {n}.Precision = 16;\nGMAT {n}.FixedWidth = false;\nGMAT {n}.Delimiter = ',';\nGMAT {n}.WriteHeaders = true;\nGMAT {n}.WriteReport = true;\nGMAT {n}.SolverIterations = None;\nGMAT {n}.Add = {{{params}}};\n"
        )
    return "\n".join(blocks)


def ingest(directory: Path, datasets: list[dict], provenance: dict, complete: bool) -> dict:
    db = sqlite3.connect(directory / "datasets.sqlite")
    manifests = []
    try:
        db.executescript(
            "CREATE TABLE IF NOT EXISTS datasets (id TEXT PRIMARY KEY, metadata TEXT NOT NULL); CREATE TABLE IF NOT EXISTS samples (dataset TEXT, seq INTEGER, epoch REAL, values_json TEXT, quality TEXT, PRIMARY KEY(dataset,seq)); CREATE INDEX IF NOT EXISTS time_index ON samples(dataset,epoch);"
        )
        for data in datasets:
            columns = data["columns"]
            count = invalid = reversals = 0
            statistics = [
                {"valid_samples": 0, "minimum": None, "maximum": None, "mean": None, "final": None}
                for _ in columns
            ]
            previous = None
            source = directory / "output" / data["filename"]
            warnings = []
            header_seen = False
            db.execute("DELETE FROM samples WHERE dataset=?", (data["id"],))
            if source.exists():
                with source.open(encoding="utf-8", errors="replace", newline="") as stream:
                    for row in csv.reader(stream):
                        if not row:
                            continue
                        if row == [c["expression"] for c in columns]:
                            header_seen = True
                            continue
                        if not header_seen:
                            warnings.append(
                                "Native header differs from the collection specification; records were not assigned to incorrect columns"
                            )
                            invalid += 1
                            break
                        if any(
                            re.fullmatch(
                                r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", token.strip()
                            )
                            for token in row
                        ):
                            warnings.append(
                                "Native columns changed during execution; remaining records were not assigned to incorrect columns"
                            )
                            invalid += 1
                            break
                        if len(row) != len(columns):
                            invalid += 1
                            continue
                        values = []
                        quality = []
                        for index, token in enumerate(row):
                            try:
                                value = float(token.replace("D", "E"))
                                if not math.isfinite(value):
                                    raise ValueError()
                            except ValueError:
                                value = None
                                quality.append(index)
                            values.append(value)
                            stat = statistics[index]
                            stat["final"] = value
                            if value is not None:
                                stat["valid_samples"] += 1
                                stat["minimum"] = (
                                    value
                                    if stat["minimum"] is None
                                    else min(stat["minimum"], value)
                                )
                                stat["maximum"] = (
                                    value
                                    if stat["maximum"] is None
                                    else max(stat["maximum"], value)
                                )
                                stat["mean"] = (
                                    value
                                    if stat["mean"] is None
                                    else stat["mean"]
                                    + (value - stat["mean"]) / stat["valid_samples"]
                                )
                        epoch = values[0]
                        if epoch is not None and previous is not None and epoch < previous:
                            reversals += 1
                        if epoch is not None:
                            previous = epoch
                        db.execute(
                            "INSERT INTO samples VALUES(?,?,?,?,?)",
                            (data["id"], count, epoch, json.dumps(values), json.dumps(quality)),
                        )
                        count += 1
            else:
                warnings.append("Native report is missing")
            if invalid:
                warnings.append(f"{invalid} malformed/truncated records; native source retained")
            metadata = {
                **data,
                **provenance,
                "rows": count,
                "malformed_rows": invalid,
                "time_reversals": reversals,
                "complete": bool(complete and source.exists() and not invalid),
                "warnings": warnings,
                "sampling": "native published samples; no interpolation",
                "native_source": f"output/{data['filename']}",
                "statistics": {c["expression"]: s for c, s in zip(columns, statistics)},
            }
            db.execute(
                "INSERT OR REPLACE INTO datasets VALUES(?,?)", (data["id"], json.dumps(metadata))
            )
            manifests.append(metadata)
        db.commit()
    finally:
        db.close()
    return {"datasets": manifests}


def connect(directory: Path):
    path = directory / "datasets.sqlite"
    if (
        not path.exists()
        or path.is_symlink()
        or not path.resolve().is_relative_to(directory.resolve())
    ):
        raise GmatError("RESULTS_UNAVAILABLE", "No collected datasets for this run")
    return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)


def list_datasets(directory: Path) -> list[dict]:
    if not (directory / "datasets.sqlite").exists():
        return []
    with connect(directory) as db:
        return [json.loads(r[0]) for r in db.execute("SELECT metadata FROM datasets ORDER BY id")]


def metadata(directory: Path, dataset_id: str) -> dict:
    with connect(directory) as db:
        row = db.execute("SELECT metadata FROM datasets WHERE id=?", (dataset_id,)).fetchone()
    if not row:
        raise GmatError("UNKNOWN_DATASET", "Dataset not found in this run")
    return json.loads(row[0])


def query(
    directory: Path, dataset_id: str, columns=None, start=None, end=None, offset=0, limit=200
) -> dict:
    if not 1 <= limit <= 1000 or offset < 0:
        raise GmatError("INVALID_RANGE", "limit must be 1..1000 and offset nonnegative")
    for value in (start, end):
        if value is not None and not math.isfinite(value):
            raise GmatError("INVALID_RANGE", "Time bounds must be finite GMAT A1ModJulian values")
    meta = metadata(directory, dataset_id)
    names = [c["expression"] for c in meta["columns"]]
    requested = columns or names
    if any(c not in names for c in requested):
        raise GmatError("INVALID_ARGUMENT", "Unknown dataset column")
    indices = [names.index(c) for c in requested]
    sql = "SELECT seq,values_json,quality FROM samples WHERE dataset=? AND seq>=?"
    args = [dataset_id, offset]
    if start is not None:
        sql += " AND epoch>=?"
        args.append(start)
    if end is not None:
        sql += " AND epoch<=?"
        args.append(end)
    sql += " ORDER BY seq LIMIT ?"
    args.append(limit + 1)
    rows = []
    size = (
        len(
            json.dumps(
                {"columns": [meta["columns"][i] for i in indices], "dataset_id": dataset_id}
            ).encode()
        )
        + 512
    )
    if size > 256 * 1024:
        raise GmatError("RESPONSE_LIMIT", "Select fewer columns")
    more = False
    next_offset = offset
    with connect(directory) as db:
        for seq, body, quality in db.execute(sql, args):
            vals = json.loads(body)
            record = {
                "index": seq,
                "values": [vals[i] for i in indices],
                "invalid_columns": [names[i] for i in json.loads(quality) if i in indices],
            }
            nbytes = len(json.dumps(record).encode())
            if len(rows) >= limit or size + nbytes + 1 > 256 * 1024:
                more = True
                break
            rows.append(record)
            size += nbytes
            next_offset = seq + 1
    return {
        "dataset_id": dataset_id,
        "columns": [meta["columns"][i] for i in indices],
        "rows": rows,
        "next_offset": next_offset,
        "has_more": more,
        "complete": meta["complete"],
        "time_system": "A1",
        "epoch_format": "GMATModJulian",
    }


def export_csv(directory: Path, dataset_id: str) -> Path:
    meta = metadata(directory, dataset_id)
    target = directory / "exports" / f"{dataset_id}.csv"
    target.parent.mkdir(exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as stream, connect(directory) as db:
        writer = csv.writer(stream)
        writer.writerow(
            [f"{c['expression']} [{c.get('unit') or 'unknown unit'}]" for c in meta["columns"]]
        )
        for (row,) in db.execute(
            "SELECT values_json FROM samples WHERE dataset=? ORDER BY seq", (dataset_id,)
        ):
            writer.writerow(json.loads(row))
    return target


def plot(directory: Path, dataset_id: str, kind="time_history", columns=None) -> dict:
    import matplotlib

    matplotlib.use("Agg")
    import numpy as np
    from matplotlib import patheffects
    from matplotlib import pyplot as plt

    meta = metadata(directory, dataset_id)
    cols = meta["columns"]
    roles = {c["role"]: i for i, c in enumerate(cols)}
    names = [c["expression"] for c in cols]
    original = meta["rows"]
    reduced = original > 10000
    values = []
    # Bound rendering memory independently of history length. Each bucket keeps
    # its endpoints and the rows containing every column's extrema.
    bucket_size = (
        max(1, math.ceil(original / max(1, 10000 // (2 * len(cols) + 2)))) if reduced else 1
    )
    selected = {}
    minima = np.full(len(cols), np.inf)
    maxima = np.full(len(cols), -np.inf)
    min_rows = {}
    max_rows = {}
    with connect(directory) as db:
        for seq, body in db.execute(
            "SELECT seq,values_json FROM samples WHERE dataset=? ORDER BY seq", (dataset_id,)
        ):
            row = json.loads(body)
            if not reduced:
                values.append(row)
                continue
            if seq % bucket_size == 0:
                selected = {seq: row}
                min_rows = {}
                max_rows = {}
                minima[:] = np.inf
                maxima[:] = -np.inf
            numeric = np.array(row, dtype=float)
            for i in np.flatnonzero(numeric < minima):
                minima[i] = numeric[i]
                min_rows[i] = (seq, row)
            for i in np.flatnonzero(numeric > maxima):
                maxima[i] = numeric[i]
                max_rows[i] = (seq, row)
            # Retain only the last endpoint; extrema are keyed by column.
            selected[-1] = (seq, row)
            if (seq + 1) % bucket_size == 0 or seq + 1 == original:
                retained = {k: v for k, v in selected.items() if k >= 0}
                retained.update(
                    {n: r for n, r in list(min_rows.values()) + list(max_rows.values())}
                )
                last = selected.get(-1)
                if last:
                    retained[last[0]] = last[1]
                values.extend(retained[i] for i in sorted(retained))
                selected = {}
                min_rows = {}
                max_rows = {}
    if not values:
        raise GmatError("RESULTS_UNAVAILABLE", "Dataset has no samples")
    a = np.array(values, dtype=float)
    segments = np.split(np.arange(len(a)), np.flatnonzero(np.diff(a[:, 0]) < 0) + 1)
    fig = plt.figure(figsize=(9, 6), layout="constrained")
    try:
        if kind == "orbit_3d":
            if not all(p in roles for p in ("X", "Y", "Z")):
                raise GmatError("INVALID_ARGUMENT", "3D orbit needs collected Cartesian positions")
            ax = fig.add_subplot(projection="3d")
            for segment in segments:
                ax.plot(*(a[segment, roles[p]] for p in ("X", "Y", "Z")))
            ax.scatter(
                [0],
                [0],
                [0],
                marker="o",
                s=30,
                label=cols[roles["X"]].get("origin") or "Frame origin",
            )
            ax.legend(fontsize=8)
            ranges = np.ptp(a[:, [roles[p] for p in ("X", "Y", "Z")]], axis=0)
            if np.isfinite(ranges).all() and ranges.max() > 0:
                ax.set_box_aspect(np.maximum(ranges, ranges.max() * 0.05))
            for p, setter in zip(("X", "Y", "Z"), (ax.set_xlabel, ax.set_ylabel, ax.set_zlabel)):
                setter(f"{p} [km]")
            ax.set_title(f"{dataset_id} — {cols[roles['X']].get('frame', 'unknown frame')}")
        elif kind == "ground_track":
            if not all(p in roles for p in ("Longitude", "Latitude")):
                raise GmatError(
                    "INVALID_ARGUMENT", "Ground track needs collected longitude and latitude"
                )
            ax = fig.add_subplot()
            provenance = directory / "provenance.json"
            if provenance.exists() and cols[roles["Longitude"]].get("origin") == "Earth":
                native = json.loads(provenance.read_text())
                root = native.get("profile", {}).get("root")
                if root:
                    texture = Path(root) / "data/graphics/texture/ModifiedBlueMarble.jpg"
                    if texture.is_file():
                        ax.imshow(
                            plt.imread(texture),
                            extent=(-180, 180, -90, 90),
                            origin="upper",
                            aspect="auto",
                            alpha=0.7,
                        )
            x, y = a[:, roles["Longitude"]], a[:, roles["Latitude"]]
            x = (x + 180) % 360 - 180
            breaks = np.flatnonzero((np.abs(np.diff(x)) > 180) | (np.diff(a[:, 0]) < 0)) + 1
            for segment in np.split(np.arange(len(x)), breaks):
                ax.plot(
                    x[segment],
                    y[segment],
                    color="#ffdf4c",
                    linewidth=1.8,
                    path_effects=[
                        patheffects.Stroke(linewidth=3, foreground="#132c44"),
                        patheffects.Normal(),
                    ],
                )
            ax.set(
                xlabel="Longitude [deg east]",
                ylabel="Latitude [deg]",
                xlim=(-180, 180),
                ylim=(-90, 90),
                title=f"{dataset_id} — {cols[roles['Longitude']].get('origin')} body-fixed",
            )
        elif kind == "time_history":
            requested = (
                columns or [c["expression"] for c in cols if c["role"] in ("SMA", "ECC", "INC")][:1]
            )
            if not requested or any(n not in names for n in requested):
                raise GmatError("INVALID_ARGUMENT", "Select collected numeric columns")
            units = {cols[names.index(n)].get("unit") for n in requested}
            if len(units) > 1:
                raise GmatError("INVALID_ARGUMENT", "Plot columns with the same units together")
            ax = fig.add_subplot()
            for name in requested:
                for i, segment in enumerate(segments):
                    ax.plot(
                        a[segment, roles["elapsed"]],
                        a[segment, names.index(name)],
                        label=name if i == 0 else None,
                    )
            ax.set(
                xlabel="Elapsed time [s]",
                ylabel=f"Value [{next(iter(units)) or 'unknown unit'}]",
                title=dataset_id,
            )
            ax.legend()
        else:
            raise GmatError(
                "INVALID_ARGUMENT", "kind must be time_history, ground_track or orbit_3d"
            )
        ax.grid(alpha=0.3)
        fig.supxlabel(
            f"Run {meta['run_id']} · native samples · "
            + (
                "display downsampled; exports retain all samples"
                if reduced
                else f"{original} samples"
            )
            + (" · INCOMPLETE" if not meta["complete"] else ""),
            fontsize=8,
        )
        stem = hashlib.sha256(json.dumps([dataset_id, kind, columns]).encode()).hexdigest()[:16]
        folder = directory / "plots"
        folder.mkdir(exist_ok=True)
        png, svg = folder / f"{stem}.png", folder / f"{stem}.svg"
        fig.savefig(png, dpi=130)
        fig.savefig(svg)
        preview = folder / f"{stem}_preview.png"
        fig.savefig(preview, dpi=85)
        return {
            "png": str(png),
            "svg": str(svg),
            "preview": str(preview),
            "display_downsampled": reduced,
            "samples": original,
        }
    finally:
        plt.close(fig)
