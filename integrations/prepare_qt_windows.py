"""Preview/apply the GMAT-Qt Yukon fix required by Windows API-enabled builds.

The Windows SDK's min/max macros expand unparenthesized std::min/max calls
when the API build exposes Windows headers. Parentheses preserve C++ behavior.
"""

import argparse
import difflib
from pathlib import Path


def prepare(root):
    path = root / "plugins/YukonOptimizerPlugin/src/base/solver/Yukon.cpp"
    old = path.read_text(encoding="utf-8")
    new = old.replace("std::min(", "(std::min)(").replace("std::max(", "(std::max)(")
    return path, old, new


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    path, old, new = prepare(args.checkout)
    if old != new:
        if args.apply:
            path.write_text(new, encoding="utf-8", newline="\n")
            print(path)
        else:
            print(
                "".join(
                    difflib.unified_diff(
                        old.splitlines(True),
                        new.splitlines(True),
                        fromfile=str(path),
                        tofile=str(path),
                    )
                ),
                end="",
            )
