"""Conservative edits to GMAT's canonical serialization, not a GMAT parser."""

from __future__ import annotations

import re

from .errors import GmatError


def statements(text: str) -> list[tuple[int, int, str]]:
    result = []
    start = None
    quoted = comment = False
    i = 0
    while i < len(text):
        char = text[i]
        if comment:
            if char == "\n":
                comment = False
            i += 1
            continue
        if not quoted and char == "%":
            comment = True
            i += 1
            continue
        if start is None and not char.isspace():
            start = i
        if char == "'":
            if quoted and i + 1 < len(text) and text[i + 1] == "'":
                i += 2
                continue
            quoted = not quoted
        if char == ";" and not quoted:
            if start is not None:
                result.append((start, i + 1, text[start : i + 1]))
            start = None
        i += 1
    if quoted or start is not None:
        raise GmatError(
            "UNSUPPORTED_EDIT", "Cannot map canonical statements safely; use script replacement"
        )
    return result


def command_spans(text: str, tree: list[dict]) -> dict[str, tuple[int, int]]:
    tokens = statements(text)
    begin = next(
        (i for i, (_, _, s) in enumerate(tokens) if s.strip() == "BeginMissionSequence;"), None
    )
    if begin is None:
        raise GmatError("UNSUPPORTED_EDIT", "Mission sequence boundary unavailable")
    spans = {}
    cursor = begin

    def walk(nodes):
        nonlocal cursor
        for node in nodes:
            kind = node.get("type", "")
            if not kind or kind == "NoOp":
                continue
            if kind == "BeginScript":
                raise GmatError(
                    "UNSUPPORTED_EDIT", "Script-event bodies require script replacement"
                )
            while cursor < len(tokens):
                a, b, source = tokens[cursor]
                cursor += 1
                word = source.strip().split(None, 1)[0].rstrip(";")
                # Assignment commands serialize as GMAT or an expression.
                if word == kind or (
                    kind in ("Assignment", "GMAT") and (word == "GMAT" or "=" in source)
                ):
                    break
            else:
                raise GmatError("UNSUPPORTED_EDIT", f"Cannot map command {kind}")
            first = a
            walk(node.get("children", []))
            end = tokens[cursor - 1][1]
            spans[node["id"]] = (first, end)

    walk(tree)
    return spans


def edit_commands(text: str, tree: list[dict], changes: list[dict]) -> str:
    """Map a batch against the original tree. Reject intersecting subtrees."""
    spans = command_spans(text, tree)
    patches = []
    for change in changes:
        op = change["op"]
        if op not in (
            "append_command",
            "insert_command",
            "replace_command",
            "remove_command",
            "move_command",
        ):
            raise GmatError("INVALID_ARGUMENT", f"Unsupported command edit: {op}")
        if op == "append_command":
            patches.append((len(text), len(text), "\n" + change["text"].strip() + "\n"))
            continue
        if change.get("command_id") not in spans:
            raise GmatError("INVALID_ARGUMENT", "Unknown or stale command ID")
        a, b = spans[change["command_id"]]
        if op in ("remove_command", "replace_command", "move_command"):
            patches.append(
                (a, b, change.get("text", "").strip() if op == "replace_command" else "")
            )
        if op in ("move_command", "insert_command"):
            if change.get("position", "before") not in ("before", "after"):
                raise GmatError("INVALID_ARGUMENT", "position must be before or after")
            target = change.get("target_id") if op == "move_command" else change["command_id"]
            if target not in spans:
                raise GmatError("INVALID_ARGUMENT", "Unknown destination command")
            c, d = spans[target]
            p = c if change.get("position", "before") == "before" else d
            patches.append(
                (
                    p,
                    p,
                    "\n" + (text[a:b] if op == "move_command" else change["text"].strip()) + "\n",
                )
            )
    removed = [(a, b) for a, b, _ in patches if a < b]
    for i, (a, b) in enumerate(removed):
        if any(max(a, c) < min(b, d) for c, d in removed[i + 1 :]):
            raise GmatError(
                "UNSUPPORTED_EDIT",
                "Command edits overlap; split the transaction or replace the script",
            )
        if any(a <= p <= b for p, q, _ in patches if p == q):
            raise GmatError("UNSUPPORTED_EDIT", "Insertion overlaps a replaced/moved subtree")
    for _, (a, b, replacement) in sorted(
        enumerate(patches), key=lambda item: (item[1][0], item[1][1], item[0]), reverse=True
    ):
        text = text[:a] + replacement + text[b:]
    return text


def remove_managed_reports(text: str, names: list[str]) -> str:
    edits = []
    for a, b, source in statements(text):
        if any(
            re.match(
                rf"(?:Create\s+ReportFile\s+{re.escape(n)}\s*;|GMAT\s+{re.escape(n)}\.)", source
            )
            for n in names
        ):
            edits.append((a, b))
    for a, b in reversed(edits):
        text = text[:a] + text[b:]
    return text
