"""Install the reviewed bridge sources into an existing GMAT-Qt checkout.

Default is a unified diff; --apply writes only the listed integration points.
"""

import argparse
import difflib
from pathlib import Path


def integration(root):
    qt = root / "src/qtgui"
    edits = {}
    for name in ("McpBridge.hpp", "McpBridge.cpp", "McpBridgeTests.cpp"):
        edits[qt / name] = (Path(__file__).parent / "gmat-qt" / name).read_text()
    edits[qt / "McpBridgeFixture.script"] = (
        Path(__file__).resolve().parents[1] / "examples/propagation.script"
    ).read_text()

    def change(name, old, new):
        path = qt / name
        text = edits.get(path, path.read_text())
        if new in text:
            return
        if text.count(old) != 1:
            raise ValueError(f"Integration point changed: {path}: {old[:60]}")
        edits[path] = text.replace(old, new, 1)

    change(
        "CMakeLists.txt",
        "target_link_libraries(GmatQtFrontend PRIVATE Qt6::Network)",
        "target_link_libraries(GmatQtFrontend PUBLIC Qt6::Network)",
    )
    path = qt / "CMakeLists.txt"
    windows_link = (
        "\nif(WIN32)\n   target_link_libraries(GmatQtFrontend PRIVATE advapi32)\nendif()\n"
    )
    if windows_link not in edits.get(path, path.read_text()):
        edits[path] = edits.get(path, path.read_text()) + windows_link
    change(
        "CMakeLists.txt",
        "add_library(GmatQtFrontend STATIC MainWindow.cpp MainWindow.hpp MainWindowDocuments.cpp",
        "add_library(GmatQtFrontend STATIC McpBridge.cpp McpBridge.hpp MainWindow.cpp MainWindow.hpp MainWindowDocuments.cpp",
    )
    change(
        "MainWindow.hpp",
        "class MainWindow : public QMainWindow",
        "class McpBridge;\nclass MainWindow : public QMainWindow",
    )
    change(
        "MainWindow.hpp",
        "private:\n   QString applyResourceSettings",
        "private:\n   friend class McpBridge;\n   McpBridge *mcpBridge=nullptr;\n   QString applyResourceSettings",
    )
    change(
        "MainWindow.cpp",
        '#include "MainWindow.hpp"',
        '#include "MainWindow.hpp"\n#include "McpBridge.hpp"',
    )
    change(
        "MainWindow.cpp",
        "   const auto directory = scriptPath.isEmpty() ? startupDirectory : QFileInfo(scriptPath).absolutePath();",
        "   const auto bridgeDirectory=mcpBridge ? mcpBridge->projectDirectory() : QString();\n   const auto directory = !bridgeDirectory.isEmpty() ? bridgeDirectory : scriptPath.isEmpty() ? startupDirectory : QFileInfo(scriptPath).absolutePath();",
    )
    change(
        "MainWindow.cpp",
        "   solverListeners->missionStarted();",
        "   if (mcpBridge) try { mcpBridge->missionStarted(); } catch (std::exception &error) { messages->appendPlainText(QString::fromUtf8(error.what())); return RunResult::Failed; }\n   solverListeners->missionStarted();",
    )
    change(
        "MainWindow.cpp",
        "   solverListeners->missionFinished(result==RunResult::Stopped,result==RunResult::Failed);",
        "   solverListeners->missionFinished(result==RunResult::Stopped,result==RunResult::Failed);\n   if (mcpBridge) try { mcpBridge->missionFinished(result==RunResult::Completed ? 1 : result==RunResult::Stopped ? -4 : -9); } catch (std::exception &error) { messages->appendPlainText(QString::fromUtf8(error.what())); }",
    )
    change(
        "main.cpp",
        '#include "MainWindow.hpp"',
        '#include "MainWindow.hpp"\n#include "McpBridge.hpp"\n#include <QStandardPaths>\n#include <memory>',
    )
    change(
        "main.cpp",
        "   parser.addHelpOption();",
        '   parser.addHelpOption();\n   parser.addOption({"mcp-bridge", "Enable authenticated local MCP desktop control"});\n   parser.addOption({"mcp-discovery-dir", "Private bridge discovery directory", "directory"});',
    )
    change(
        "main.cpp",
        "   QTimer::singleShot(0, &window, [&] {",
        '   std::unique_ptr<McpBridge> mcp;\n   if (parser.isSet("mcp-bridge") && initialized && loaded) {\n      const auto directory=parser.isSet("mcp-discovery-dir") ? parser.value("mcp-discovery-dir") : QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation)+"/gmat-mcp/desktops";\n      try { mcp=std::make_unique<McpBridge>(window,directory); } catch (std::exception &error) { std::fprintf(stderr,"MCP bridge unavailable: %s\\n",error.what()); return 2; }\n   }\n   QTimer::singleShot(0, &window, [&] {',
    )
    change("QtSolverListener.hpp", "#include <map>", "#include <map>\n#include <QJsonArray>")
    change(
        "QtSolverListener.hpp",
        "   void missionStarted();",
        "   QJsonArray snapshot() const;\n   void missionStarted();",
    )
    change("QtSolverListener.cpp", "#include <cmath>", "#include <cmath>\n#include <QJsonObject>")
    path = qt / "QtSolverListener.cpp"
    method = """
QJsonArray QtSolverListenerManager::snapshot() const
{
   QJsonArray result;
   for (const auto &entry:listeners) {
      const auto &listener=*entry.second;
      if (!listener.active) continue;
      QJsonArray goals,variables;
      if (listener.table) for(int row=0;row<listener.table->rowCount();++row) {
         const auto text=[&](int col) { const auto *cell=listener.table->item(row,col);return cell?cell->text():QString(); };
         bool numeric=false;const auto current=text(2).toDouble(&numeric);
         QJsonObject value{{"name",text(1)},{"value",numeric?QJsonValue(current):QJsonValue(text(2))}};
         if(text(0)=="Variable") variables.append(value);
         else { value["desired"]=text(3).isEmpty()?QJsonValue():QJsonValue(text(3).toDouble());value["residual"]=text(4).isEmpty()?QJsonValue():QJsonValue(text(4).toDouble());goals.append(value); }
      }
      const auto known=listener.status && listener.status->property("converged").isValid();
      const auto convergence=known?(listener.status->property("converged").toBool()?"converged":"not_converged"):"unavailable";
      result.append(QJsonObject{{"name",QString::fromStdString(entry.first).section('\\n',0,0)},{"convergence",convergence},{"native_status",QJsonValue()},{"variables",variables},{"goals",goals},{"iterations",QJsonValue()},{"source","Qt native solver listener"}});
   }
   return result;
}
"""
    if "QtSolverListenerManager::snapshot()" not in edits.get(path, path.read_text()):
        edits[path] = edits.get(path, path.read_text()) + method
    path = qt / "CMakeLists.txt"
    if "add_executable(GmatQtMcpBridgeTests" not in edits.get(path, path.read_text()):
        edits[path] = (
            edits.get(path, path.read_text())
            + "\nif(GMAT_QT_BUILD_TESTS)\n   add_executable(GmatQtMcpBridgeTests McpBridgeTests.cpp)\n   target_link_libraries(GmatQtMcpBridgeTests PRIVATE GmatQtFrontend)\nendif()\n"
        )
    if "add_test(NAME QtGui.McpBridge" not in edits.get(path, path.read_text()):
        edits[path] = (
            edits.get(path, path.read_text())
            + """
if(GMAT_QT_BUILD_TESTS)
   add_dependencies(GmatQtMcpBridgeTests GmatQt)
   if(Python3_Interpreter_FOUND AND QT_XVFB_RUN)
      add_test(NAME QtGui.McpBridge COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/RunNativeTest.py" "${QT_XVFB_RUN}"
         ${CMAKE_COMMAND} -E env QT_QPA_PLATFORM=xcb LIBGL_ALWAYS_SOFTWARE=1
         $<TARGET_FILE:GmatQtMcpBridgeTests> "${qt_test_startup}" "${CMAKE_CURRENT_SOURCE_DIR}/McpBridgeFixture.script")
   else()
      add_test(NAME QtGui.McpBridge COMMAND ${CMAKE_COMMAND} -E env QT_QPA_PLATFORM=offscreen
         $<TARGET_FILE:GmatQtMcpBridgeTests> "$<TARGET_FILE_DIR:GmatQt>/gmat_startup_qt.txt" "${CMAKE_CURRENT_SOURCE_DIR}/McpBridgeFixture.script")
   endif()
   set_tests_properties(QtGui.McpBridge PROPERTIES LABELS qt-gui TIMEOUT 60)
endif()
"""
        )
    # Reuse the Qt repository's Windows launcher: tests live in the build tree,
    # while the engine and Qt DLLs live in their own runtime directories.
    old = "   if(Python3_Interpreter_FOUND AND QT_XVFB_RUN)\n      add_test(NAME QtGui.McpBridge"
    new = '   if(WIN32 AND Python3_Interpreter_FOUND)\n      add_qt_test(QtGui.McpBridge GmatQtMcpBridgeTests "$<TARGET_FILE_DIR:GmatQt>/gmat_startup_qt.txt" "${CMAKE_CURRENT_SOURCE_DIR}/McpBridgeFixture.script")\n   elseif(Python3_Interpreter_FOUND AND QT_XVFB_RUN)\n      add_test(NAME QtGui.McpBridge'
    text = edits.get(path, path.read_text())
    if new not in text:
        if text.count(old) != 1:
            raise ValueError("Bridge test integration point changed")
        edits[path] = text.replace(old, new, 1)
    windows_runtime = """
if(GMAT_QT_BUILD_TESTS AND WIN32)
   set_property(TEST QtGui.McpBridge PROPERTY ENVIRONMENT "QT_PLUGIN_PATH=$<TARGET_FILE_DIR:GmatQt>")
endif()
"""
    if windows_runtime not in edits.get(path, path.read_text()):
        edits[path] = edits.get(path, path.read_text()) + windows_runtime
    return edits


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    for path, new in integration(args.checkout).items():
        old = path.read_text() if path.exists() else ""
        if old == new:
            continue
        if args.apply:
            path.write_text(new)
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
