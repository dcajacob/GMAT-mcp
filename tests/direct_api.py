"""Independent reference execution, launched with the installation's interpreter.

This deliberately does not import the MCP worker or session manager.
"""

import importlib
import json
import os
import sys
from pathlib import Path


def main():
    binary, startup, script, destination = map(Path, sys.argv[1:])
    sys.path.insert(0, str(binary))
    dlls = []
    if sys.platform == "win32":
        for directory in (binary, binary.parent / "lib", binary.parent / "plugins"):
            if directory.is_dir():
                dlls.append(os.add_dll_directory(str(directory)))
    import gmatpy as g

    native = importlib.import_module(
        f"gmatpy._py{sys.version_info.major}{sys.version_info.minor}.gmat_py"
    )._gmat_py
    g.Setup(str(startup))
    g.EchoLogFile(False)
    os.chdir(script.parent)
    assert g.LoadScript(str(script))
    g.Initialize()
    code = g.Execute()
    sat = g.GetRuntimeObject("Sat")
    destination.write_text(
        json.dumps(
            {
                "native_status": code,
                "state": [
                    sat.GetRealParameter(field) for field in ("X", "Y", "Z", "VX", "VY", "VZ")
                ],
                "epoch": sat.GetField("Epoch"),
                "summary": native.GetRunSummary(),
            }
        )
    )


if __name__ == "__main__":
    main()
