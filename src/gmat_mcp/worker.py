"""Standalone stdlib-only GMAT worker, run with the profile's Python interpreter.

No native pointers, executable Python expressions, or arbitrary method names cross IPC.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import platform
import sys
import traceback
from pathlib import Path

try:
    from .installation import library_directories
    from .native_platform import binding_architectures, current_architecture
except ImportError:
    from installation import library_directories
    from native_platform import binding_architectures, current_architecture


def finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (list, tuple)):
        return [finite(item) for item in value]
    if isinstance(value, dict):
        return {key: finite(item) for key, item in value.items()}
    return value


class Engine:
    def __init__(self, bin_dir: Path, startup: Path):
        if sys.version_info < (3, 11):
            raise RuntimeError("GMAT workers require Python 3.11 or newer")
        sys.path.insert(0, str(bin_dir))
        self.dll_handles = []
        if sys.platform == "win32":
            for directory in (bin_dir, bin_dir.parent / "lib", bin_dir.parent / "plugins"):
                if directory.is_dir():
                    self.dll_handles.append(os.add_dll_directory(str(directory)))
        bindings = bin_dir / "gmatpy" / f"_py{sys.version_info.major}{sys.version_info.minor}"
        if not bindings.is_dir():
            available = sorted(p.name for p in (bin_dir / "gmatpy").glob("_py*"))
            raise RuntimeError(
                f"No GMAT bindings for Python {sys.version_info.major}.{sys.version_info.minor} "
                f"in {bin_dir}. Available: {available}. Select a matching python_executable; "
                "for GMAT-Qt build with -DGMAT_INCLUDE_API=ON -DAPI_GENERATE_PYTHON=ON."
            )
        expected = binding_architectures(bindings)
        if expected is not None and current_architecture() not in expected:
            raise RuntimeError(
                f"GMAT bindings support {sorted(expected)} but this interpreter is {current_architecture()}; select a matching interpreter architecture"
            )
        if sys.platform == "win32":
            self.dll_handles.append(os.add_dll_directory(str(bindings)))
        try:
            import gmatpy
        except (ImportError, OSError) as exc:
            hint = (
                " Complete this installation's Python API build, including station and "
                "navigation wrappers required by its gmatpy package; build the default "
                "Release target rather than only the core API target."
                if isinstance(exc, ModuleNotFoundError)
                else " On Windows, install the Microsoft Visual C++ x64 runtime required by GMAT "
                "and keep the installation's DLLs together."
                if sys.platform == "win32"
                else " On macOS use matching arm64/x86_64 CPython and GMAT libraries. "
                "Use a matching Homebrew or python.org interpreter; inspect unresolved dylibs with otool -L."
                if sys.platform == "darwin"
                else " Keep this installation's native libraries and plugins together."
            )
            raise RuntimeError(
                f"Could not import GMAT from {bin_dir} using Python "
                f"{sys.version_info.major}.{sys.version_info.minor} ({current_architecture()}): "
                f"{exc}.{hint}"
            ) from exc

        self.g = gmatpy
        # SWIG's convenience Python functions print Help/ShowClasses/ShowObjects
        # and discard their return values. Use the wrapped native functions for data.
        wrapper = importlib.import_module(
            f"gmatpy._py{sys.version_info.major}{sys.version_info.minor}.gmat_py"
        )
        self.native = wrapper._gmat_py
        required = ("Setup", "Construct", "Command", "Execute", "GetRuntimeObject", "SaveScript")
        missing = [name for name in required if not hasattr(gmatpy, name)]
        if missing:
            raise RuntimeError(f"GMAT installation is missing API functions: {missing}")
        self.g.Setup(str(startup))
        self.g.EchoLogFile(False)
        # Setup alone leaves ScriptInterpreter's SolarSystem pointer unset in
        # R2026a. SaveScript on a mission built solely through Construct/Command
        # can consequently segfault. Initialize the interpreter before any edits.
        bootstrap = startup.parent / "empty.script"
        bootstrap.write_text("BeginMissionSequence;\n", encoding="utf-8")
        if not self.g.LoadScript(str(bootstrap)):
            raise RuntimeError("GMAT could not initialize an empty mission")
        self.commands = {}
        self.next_command = 0
        self.revision = 0
        self.runtime_revision = None
        self.managed_spec = None

    def hello(self):
        classes = self.native.ShowClasses().split()
        native = []
        libraries = {
            p.resolve()
            for directory in library_directories(Path(self.g.__file__).parent.parent)
            for p in directory.glob("*GmatBase*")
            if p.is_file()
        }
        for library in sorted(libraries):
            native.append(
                {"path": str(library), "sha256": hashlib.sha256(library.read_bytes()).hexdigest()}
            )
        suffix = (
            ".dll" if sys.platform == "win32" else ".dylib" if sys.platform == "darwin" else ".so"
        )
        plugins = []
        for plugin in self.g.FileManager.Instance().GetPluginList():
            present = Path(plugin).is_file() or Path(plugin + suffix).is_file()
            loaded = bool(self.g.Moderator.Instance().IsLibraryLoaded(Path(plugin).name))
            plugins.append(
                {
                    "path": plugin,
                    "status": "loaded" if loaded else "not_loaded" if present else "missing",
                }
            )
        return {
            "python": sys.version,
            "architecture": current_architecture(),
            "platform": platform.platform(),
            "api": self.g.__file__,
            "version": self.g.GmatGlobal.Instance().GetGmatVersion(),
            "build_date": self.g.GmatGlobal.Instance().GetGMATBuildDate(),
            "plugins": plugins,
            "native_libraries": native,
            "classes": classes,
            "capabilities": [
                "objects",
                "commands",
                "scripts",
                "runtime_state",
                "execution",
                "native_reports",
                "solver_status",
            ],
            "solver_fields": ["native_status", "convergence", "variables", "report"],
        }

    def changed(self):
        self.revision += 1
        self.runtime_revision = None

    def object(self, name, view="configured"):
        if view not in ("configured", "runtime"):
            raise ValueError("view must be configured or runtime")
        if view == "runtime" and self.runtime_revision != self.revision:
            raise ValueError("Runtime state is unavailable or invalidated by a model edit")
        obj = self.g.GetRuntimeObject(name) if view == "runtime" else self.g.GetObject(name)
        if obj is None:
            raise ValueError(f"Unknown {view} object: {name}")
        return obj

    def field(self, obj, name):
        index = obj.GetParameterID(name)
        kind = obj.GetParameterTypeString(index)
        raw = obj.GetField(index)
        readers = {
            "Real": "GetRealParameter",
            "Integer": "GetIntegerParameter",
            "Boolean": "GetBooleanParameter",
            "String": "GetStringParameter",
            "Filename": "GetStringParameter",
            "Enumeration": "GetStringParameter",
            "Object": "GetStringParameter",
            "StringArray": "GetStringArrayParameter",
            "ObjectArray": "GetStringArrayParameter",
        }
        value = raw
        typed = False
        if kind in readers:
            try:
                value = getattr(obj, readers[kind])(index)
                if kind.endswith("Array"):
                    value = list(value)
                typed = True
            except Exception:
                value = raw
        try:
            read_only = bool(obj.IsParameterReadOnly(index))
        except Exception:
            # Spacecraft aliases such as X have synthetic IDs for which some
            # releases implement getters/setters but no writability metadata.
            read_only = None
        try:
            unit = obj.GetParameterUnit(index)
        except Exception:
            unit = None
        return {
            "name": name,
            "type": kind,
            "unit": unit,
            "read_only": read_only,
            "value": value,
            "text": raw,
            "typed": typed,
        }

    def get_object(self, name, fields=None, view="configured"):
        obj = self.object(name, view)
        names = (
            fields
            if fields is not None
            else [obj.GetParameterText(index) for index in range(obj.GetParameterCount())]
        )
        values = []
        for field in names:
            try:
                values.append(self.field(obj, field))
            except Exception as exc:
                if fields is not None:
                    raise
                values.append({"name": field, "error": str(exc)})
        context = {}
        if obj.IsOfType("Spacecraft"):
            for field in ("Epoch", "DateFormat", "CoordinateSystem", "DisplayStateType"):
                try:
                    context[field] = obj.GetField(field)
                except Exception:
                    pass
            # Epoch is returned by GMAT in the selected DateFormat; never invent a conversion.
        return {
            "name": name,
            "type": obj.GetTypeName(),
            "view": view,
            "revision": self.revision,
            "context": context,
            "fields": values,
        }

    def create_object(self, type, name, constructor_args=None):
        if self.g.Exists(name):
            raise ValueError(f"Object already exists: {name}")
        args = constructor_args or []
        if len(args) > 4 or not name:
            raise ValueError("A name and at most four constructor arguments are required")
        self.changed()
        obj = self.g.Construct(type, name, *args)
        if obj is None:
            raise ValueError(f"Cannot construct GMAT class: {type}")
        return self.get_object(name)

    def set_field(self, name, field, value):
        numbers = value if isinstance(value, list) else [value]
        if any(isinstance(item, float) and not math.isfinite(item) for item in numbers):
            raise ValueError("Numeric field values must be finite")
        obj = self.object(name)
        index = obj.GetParameterID(field)
        if self.field(obj, field)["read_only"] is True:
            raise ValueError(f"Read-only field: {name}.{field}")
        self.changed()  # Failed native setters can still partially modify state.
        if isinstance(value, list):
            kind = obj.GetParameterTypeString(index)
            container = (
                self.g.StringArray()
                if kind in ("StringArray", "ObjectArray")
                else self.g.RealArray()
            )
            for item in value:
                container.push_back(item)
            obj.SetField(index, container)
        elif isinstance(value, (str, int, float, bool)):
            obj.SetField(index, value)
        else:
            raise ValueError(
                "Field values must be scalars or supported GMAT arrays; use scripts for complex fields"
            )
        return {"revision": self.revision, "field": self.field(obj, field)}

    def list_objects(self, type=""):
        kind = self.g.UNKNOWN_OBJECT if not type else self.g.GmatBase.GetObjectType(type)
        if type and kind == self.g.UNKNOWN_OBJECT:
            raise ValueError(f"Unknown GMAT object base type: {type}")
        return {
            "objects": list(self.g.Moderator.Instance().GetListOfObjects(kind)),
            "revision": self.revision,
        }

    def list_classes(self, type="UnknownObject"):
        return {"classes": self.native.ShowClasses(type).split()}

    def get_help(self, topic=""):
        return {"text": self.native.Help(topic)[:65536]}

    def command_tree(self, commands, depth=0, seen=None):
        if depth > 32:
            return [{"error": "Command nesting exceeds display limit"}]
        seen = seen if seen is not None else set()
        result = []
        for command in commands:
            pointer = int(command.this)
            if pointer in seen:
                continue
            seen.add(pointer)
            key = str(pointer)
            if key not in self.commands:
                self.next_command += 1
                self.commands[key] = f"command-{self.next_command}"
            result.append(
                {
                    "id": self.commands[key],
                    "type": command.GetTypeName(),
                    "text": command.GetGeneratingString(),
                    "children": self.command_tree(
                        self.g.GetNestedCommands(command), depth + 1, seen
                    ),
                }
            )
        return result

    def list_commands(self):
        return {"commands": self.command_tree(self.g.GetCommands()), "revision": self.revision}

    def append_command(self, type, description=""):
        self.changed()
        command = self.g.Command(type, description)
        if command is None:
            raise ValueError(f"GMAT rejected command {type}: {description}; inspect native logs")
        return {
            "type": command.GetTypeName(),
            "text": command.GetGeneratingString(),
            "revision": self.revision,
        }

    def load_script(self, path, project_dir):
        markers = [
            line[len("% GMAT-MCP-Results ") :]
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.startswith("% GMAT-MCP-Results ")
        ]
        if len(markers) > 1:
            raise ValueError("Ambiguous results specification")
        self.managed_spec = json.loads(markers[0]) if markers else None
        os.chdir(project_dir)
        self.changed()
        if Path(path).parent == Path(project_dir):
            parsed = self.g.LoadScript(str(path))
        else:
            # LoadScript unconditionally replaces GMAT's working directory with
            # the input file's parent. For staged text / explicit project roots,
            # use the same native parser on this fresh, empty replacement engine
            # while keeping the requested input resolution directory.
            moderator = self.g.Moderator.Instance()
            moderator.ClearCommandSeq()
            self.g.FileManager.Instance().SetGmatWorkingDirectory(str(project_dir) + os.sep)
            parsed = moderator.GetScriptInterpreter().Interpret(str(path))
        if not parsed:
            raise ValueError("GMAT script parsing failed; inspect native diagnostics")
        return {
            "parsing": "passed",
            "initialization": "not_checked",
            "revision": self.revision,
            "results_spec": self.managed_spec,
        }

    def export_script(self, path):
        settings = self.g.GmatGlobal.Instance()
        precision = settings.GetDataPrecision() if hasattr(settings, "GetDataPrecision") else None
        time_precision = (
            settings.GetTimePrecision() if hasattr(settings, "GetTimePrecision") else None
        )
        presentation = []
        try:
            if precision is not None:
                settings.SetDataPrecision(17)
            if time_precision is not None:
                settings.SetTimePrecision(17)
            # Serializing a reconstructed Keplerian state loses Cartesian ULPs;
            # Gregorian display can also truncate sub-millisecond epochs. Emit
            # authoritative Cartesian/A1 values, then restore display preferences.
            if hasattr(self.g, "SPACECRAFT"):
                for name in self.g.Moderator.Instance().GetListOfObjects(self.g.SPACECRAFT):
                    obj = self.g.GetObject(name)
                    old_state = obj.GetStringParameter("DisplayStateType")
                    old_date = obj.GetStringParameter("DateFormat")
                    presentation.append((name, obj, old_state, old_date))
                    obj.SetField("DisplayStateType", "Cartesian")
                    obj.SetField("DateFormat", "A1ModJulian")
            if not self.g.SaveScript(str(path)):
                raise ValueError("GMAT could not export the model")
        finally:
            for name, obj, old_state, old_date in presentation:
                obj.SetField("DisplayStateType", old_state)
                obj.SetField("DateFormat", old_date)
            if precision is not None:
                settings.SetDataPrecision(precision)
            if time_precision is not None:
                settings.SetTimePrecision(time_precision)
        if presentation:
            text = Path(path).read_text(encoding="utf-8")
            restore = "\n".join(
                f"GMAT {name}.DisplayStateType = {state};\nGMAT {name}.DateFormat = {date};"
                for name, _, state, date in presentation
            )
            text = text.replace("BeginMissionSequence;", restore + "\nBeginMissionSequence;", 1)
            Path(path).write_text(text, encoding="utf-8")
        if self.managed_spec is not None:
            text = Path(path).read_text(encoding="utf-8")
            text = (
                "\n".join(
                    line for line in text.splitlines() if not line.startswith("% GMAT-MCP-Results ")
                )
                + "\n"
            )
            Path(path).write_text(
                "% GMAT-MCP-Results "
                + json.dumps(self.managed_spec, separators=(",", ":"))
                + "\n"
                + text,
                encoding="utf-8",
            )
        return {"text": Path(path).read_text(encoding="utf-8"), "revision": self.revision}

    def validate(self):
        self.g.Initialize()
        return {
            "parsing": "not_rechecked",
            "object_initialization": "passed",
            "execution": "not_checked",
            "solver_convergence": "not_checked",
            "revision": self.revision,
        }

    def parameter_metadata(self, expressions):
        parameters = []
        for expression in expressions:
            obj = self.g.GetObject(expression)
            if obj is None:
                raise ValueError(f"Unknown reportable parameter: {expression}")
            try:
                unit = obj.GetStringParameter("Unit")
            except Exception:
                unit = None
            meta = {"unit": unit, "unit_source": "native metadata" if unit else "unavailable"}
            parts = expression.split(".")
            if len(parts) == 3:
                dependency = self.g.GetObject(parts[1])
                if dependency is not None:
                    try:
                        if dependency.IsOfType("CoordinateSystem"):
                            meta.update(
                                frame=parts[1], origin=dependency.GetStringParameter("Origin")
                            )
                        elif dependency.IsOfType("CelestialBody"):
                            meta["origin"] = parts[1]
                    except Exception:
                        pass
            parameters.append(meta)
        return {"parameters": parameters}

    def solver_results(self):
        results = []
        if not hasattr(self.g, "SOLVER"):
            return results
        clones = {}
        for command in getattr(self, "solver_branches", []):
            try:
                clone = command.GetClone(0)
                if clone is not None:
                    clones[clone.GetName()] = clone
            except Exception:
                pass
        for name in self.g.Moderator.Instance().GetListOfObjects(self.g.SOLVER):
            configured = self.g.GetObject(name)
            obj = clones.get(name) or self.g.GetRuntimeObject(name)
            record = {
                "name": name,
                "type": configured.GetTypeName(),
                "native_status": None,
                "convergence": "unavailable",
                "variables": [],
                "goals": None,
                "iterations": None,
                "source": "native runtime metadata",
            }
            if obj is not None:
                try:
                    status = obj.GetIntegerParameter("IntegerSolverStatus")
                    record.update(
                        native_status=status,
                        convergence="converged"
                        if status in (1, 5)
                        else "not_converged"
                        if status < 0 and status != -10
                        else "not_run"
                        if status in (0, 2, 3)
                        else "unavailable",
                    )
                except Exception:
                    pass
                try:
                    record["variables"] = list(obj.GetStringArrayParameter("Variables"))
                except Exception:
                    pass
                try:
                    record["report"] = obj.GetStringParameter("ReportFile")
                except Exception:
                    pass
            results.append(record)
        return results

    def execute(self, output_dir, log_file):
        manager = self.g.FileManager.Instance()
        for key in ("OUTPUT_PATH", "MEASUREMENT_PATH", "VEHICLE_EPHEM_PATH"):
            manager.SetAbsPathname(key, str(output_dir) + os.sep)
        # ReportFile resolves and caches its full path while parsing, before
        # the run directory exists. Refresh the cache after changing OUTPUT_PATH.
        for name in self.g.Moderator.Instance().GetListOfObjects(self.g.UNKNOWN_OBJECT):
            obj = self.g.GetObject(name)
            if obj.IsOfType("ReportFile") or obj.IsOfType("EphemerisFile"):
                obj.SetField("Filename", obj.GetStringParameter("Filename"))
        self.g.UseLogFile(str(log_file))
        self.g.EchoLogFile(False)
        self.runtime_revision = None
        self.solver_branches = []

        def capture(commands):
            for command in commands:
                if command.GetTypeName() in ("Target", "Optimize"):
                    self.solver_branches.append(command)
                if command.GetTypeName() not in ("EndTarget", "EndOptimize"):
                    capture(self.g.GetNestedCommands(command))

        capture(self.g.GetCommands())
        self.g.Initialize()
        status = self.g.Execute()
        # On normal native failure GMAT may retain partial runtime objects.
        self.runtime_revision = self.revision
        try:
            summary = self.native.GetRunSummary()
        except Exception as exc:
            summary = str(exc)
        Path(log_file).with_name("summary.txt").write_text(summary, encoding="utf-8", newline="\n")
        return {
            "native_status": status,
            "summary": summary[:65536],
            "summary_truncated": len(summary) > 65536,
            "revision": self.revision,
            "solver_convergence": "inspect_summary_and_outputs",
            "solver_results": self.solver_results(),
        }


def main():
    if os.environ.get("GMAT_MCP_START_GATE") == "1":
        if json.loads(sys.stdin.readline() or "{}").get("operation") != "_start":
            return 1
    # Duplicate the original pipe first, then redirect fd 1 itself. This captures
    # C/C++ printf/cout as well as Python prints, including import-time diagnostics.
    protocol = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)
    if sys.platform == "win32":
        import ctypes
        import msvcrt

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.SetStdHandle.argtypes = [ctypes.c_ulong, ctypes.c_void_p]
        kernel.SetStdHandle.restype = ctypes.c_int
        if not kernel.SetStdHandle(0xFFFFFFF5, msvcrt.get_osfhandle(2)):
            raise ctypes.WinError(ctypes.get_last_error())

    def send(payload):
        protocol.write(json.dumps(finite(payload), allow_nan=False) + "\n")
        protocol.flush()

    try:
        engine = Engine(Path(sys.argv[1]), Path(sys.argv[2]))
        send({"ok": True, "result": engine.hello()})
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        send({"ok": False, "error": {"code": "ENGINE_SETUP", "message": str(exc)}})
        return 1
    operations = {
        name: getattr(engine, name)
        for name in (
            "get_object",
            "create_object",
            "set_field",
            "list_objects",
            "list_classes",
            "get_help",
            "list_commands",
            "append_command",
            "load_script",
            "export_script",
            "validate",
            "execute",
            "parameter_metadata",
        )
    }
    for line in sys.stdin:
        try:
            request = json.loads(line)
            operation = operations[request["operation"]]
            result = operation(**request.get("arguments", {}))
            send({"ok": True, "result": result, "revision": engine.revision})
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            send(
                {
                    "ok": False,
                    "error": {"code": "GMAT_ERROR", "message": str(exc)},
                    "revision": engine.revision,
                }
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
