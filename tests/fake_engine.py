"""Deterministic native API double used only by portable lifecycle/protocol tests."""

import copy
import json
import os
import time
from pathlib import Path

objects = {}
runtime = {}
commands = []
paths = {}
scenario = "normal"
plugins = []
UNKNOWN_OBJECT = 0
RealArray = list
StringArray = list


class GmatGlobal:
    @staticmethod
    def Instance():
        return GmatGlobal()

    def GetGmatVersion(self):
        return "FAKE-test-only"

    def GetGMATBuildDate(self):
        return "test fixture"


class GmatBase:
    @staticmethod
    def GetObjectType(type):
        return type if type in ("Spacecraft", "ReportFile") else 0


class Moderator:
    @staticmethod
    def Instance():
        return Moderator()

    def GetListOfObjects(self, type):
        return [name for name, obj in objects.items() if not type or obj.kind == type]

    def IsLibraryLoaded(self, plugin):
        return False

    def ClearCommandSeq(self):
        commands.clear()

    def GetScriptInterpreter(self):
        return ScriptInterpreter()


class ScriptInterpreter:
    def Interpret(self, path):
        return LoadScript(path)


class FileManager:
    @staticmethod
    def Instance():
        return FileManager()

    def SetAbsPathname(self, key, value):
        paths[key] = value

    def GetPluginList(self):
        return plugins

    def SetGmatWorkingDirectory(self, path):
        paths["project"] = path


class Obj:
    def __init__(self, kind, name):
        self.kind, self.name = kind, name
        self.fields = {
            "X": 1.0,
            "Y": 0.0,
            "Z": 0.0,
            "DryMass": 100.0,
            "DateFormat": "UTCGregorian",
            "Epoch": "01 Jan 2026 00:00:00.000",
            "CoordinateSystem": "EarthMJ2000Eq",
            "DisplayStateType": "Cartesian",
            "Locked": "locked",
        }

    def GetParameterID(self, name):
        if name not in self.fields:
            raise ValueError("Unknown field: " + name)
        return name

    def GetParameterCount(self):
        return len(self.fields)

    def GetParameterText(self, index):
        return list(self.fields)[index]

    def GetParameterTypeString(self, index):
        return "Real" if isinstance(self.fields[index], float) else "String"

    def GetParameterUnit(self, index):
        return "km" if index in ("X", "Y", "Z") else ""

    def GetField(self, index):
        return str(self.fields[index])

    def GetRealParameter(self, index):
        return float(self.fields[index])

    def GetStringParameter(self, index):
        return self.fields[index]

    def IsParameterReadOnly(self, index):
        return index == "Locked"

    def IsOfType(self, name):
        return self.kind == name

    def GetTypeName(self):
        return self.kind

    def SetField(self, index, value):
        if index == "DryMass" and value < 0:
            raise ValueError("DryMass must be positive")
        self.fields[index] = value


class Cmd:
    def __init__(self, kind, description):
        self.kind, self.description, self.this = kind, description, len(commands) + 1

    def GetTypeName(self):
        return self.kind

    def GetGeneratingString(self):
        return self.kind + " " + self.description


def Setup(startup):
    plugins[:] = [
        line.split("=", 1)[1].strip()
        for line in Path(startup).read_text().splitlines()
        if line.startswith("PLUGIN =")
    ]
    os.write(1, b"native import/setup stdout must not corrupt JSON\n")


def EchoLogFile(value):
    pass


def UseLogFile(path):
    Path(path).write_text("fake engine log\n")


def ShowClasses(type=""):
    return "Spacecraft Propagator ReportFile"


def Help(topic=""):
    return "Help for " + topic


def Construct(type, name, *args):
    objects[name] = Obj(type, name)
    return objects[name]


def Exists(name):
    return name in objects


def GetObject(name):
    return objects.get(name)


def GetRuntimeObject(name):
    return runtime.get(name)


def Command(type, description=""):
    command = Cmd(type, description)
    commands.append(command)
    return command


def GetCommands():
    return commands


def GetNestedCommands(command):
    return []


def Initialize():
    pass


def LoadScript(path):
    global scenario
    text = Path(path).read_text()
    if text == "badparse":
        print("script syntax error", flush=True)
        return False
    objects.clear()
    if text.strip() == "BeginMissionSequence;":
        return True
    if text.startswith("{"):
        data = json.loads(text)
        scenario = data["scenario"]
        for name, fields in data["objects"].items():
            Construct("Spacecraft", name).fields = fields
    else:
        scenario = text.strip()
        Construct("Spacecraft", "Sat")
    return True


def SaveScript(path):
    Path(path).write_text(
        json.dumps(
            {"scenario": scenario, "objects": {name: obj.fields for name, obj in objects.items()}}
        )
    )
    return True


def Execute():
    if scenario == "slow":
        print("starting slow mission", flush=True)
        time.sleep(30)
    if scenario == "crash":
        os._exit(77)
    print("executing native mission", flush=True)
    runtime.clear()
    runtime.update(copy.deepcopy(objects))
    for obj in runtime.values():
        obj.fields["X"] += 1
    Path(paths["OUTPUT_PATH"], "report.txt").write_text("state = 2\n")
    return -5 if scenario == "failure" else 1


def GetRunSummary():
    if scenario == "longsummary":
        return "long summary\n" * 10000
    return "fake summary"
