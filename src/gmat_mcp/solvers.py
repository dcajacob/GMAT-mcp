"""R2026a DifferentialCorrector report adapter. Final records only."""

import re
from pathlib import Path

NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"


def enrich(records: list[dict], directory: Path, engine_version: str):
    if "R2026a" not in engine_version:
        return records
    for record in records:
        report = record.get("report")
        if record.get("type") != "DifferentialCorrector" or not report:
            continue
        path = Path(report)
        if not path.is_absolute():
            path = directory / "output" / path
        if not path.is_file():
            continue
        # Report paths retain native semantics. Only consume retained artifacts.
        if not path.resolve().is_relative_to(directory.resolve()):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        iterations = re.findall(r"Targeting Completed in\s+(\d+)\s+iterations", text)
        if iterations:
            record["iterations"] = int(iterations[-1])
        blocks = re.split(r"Iteration\s+\d+\s*\nRunning Nominal Pass", text)
        if len(blocks) < 2:
            continue
        last = blocks[-1].split("Jacobian", 1)[0]
        goals = []
        for match in re.finditer(
            rf"([^\n]+?)\s+Desired:\s*({NUMBER})\s+Achieved:\s*({NUMBER})\s*\n\s*Tolerance:\s*({NUMBER})",
            last,
        ):
            name, desired, achieved, tolerance = match.groups()
            desired, achieved, tolerance = map(float, (desired, achieved, tolerance))
            goals.append(
                {
                    "name": name.strip(),
                    "desired": desired,
                    "achieved": achieved,
                    "residual": achieved - desired,
                    "tolerance": tolerance,
                    "source": "R2026a DifferentialCorrector report",
                }
            )
        variables = []
        section = last.split("Variables:", 1)[-1].split("Goals and achieved values:", 1)[0]
        for match in re.finditer(rf"^\s*([A-Za-z][\w.]*)\s*=\s*({NUMBER})\s*$", section, re.M):
            variables.append(
                {
                    "name": match[1],
                    "value": float(match[2]),
                    "source": "R2026a DifferentialCorrector report",
                }
            )
        if goals:
            record["goals"] = goals
        if variables:
            record["variables"] = variables
        record["report_artifact"] = path.relative_to(directory).as_posix()
    return records
