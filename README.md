# GMAT MCP

Interactive MCP mission design for NASA GMAT R2026a and GMAT-Qt, with isolated
workers, sampled results, transactional editing, saved projects, trade studies
and an optional live GMAT-Qt desktop connection.
Linux and Windows have native qualification results. macOS support is undergoing
native Apple Silicon and Intel qualification on GitHub Actions. This project does
not bundle GMAT. See [qualification evidence](VALIDATION.md) for current results.

## Install

Use Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/):

```sh
git clone https://github.com/dcajacob/GMAT-mcp.git
cd GMAT-mcp
uv sync --locked
uv run gmat-mcp discover --root /path/to/GMAT --root /path/to/GMAT-Qt/application
uv run gmat-mcp init --root /path/to/GMAT --root /path/to/GMAT-Qt/application --output profiles.local.toml
uv run gmat-mcp --config profiles.local.toml doctor
```

Each profile's `root` contains `bin/`, `data/`, `plugins/` and `api/` (typically
`application/` in a source checkout). Its interpreter must match a directory such
as `bin/gmatpy/_py312`. The worker needs only Python's standard library and the
installation's native GMAT bindings; it does not need the MCP SDK installed.
`doctor` reports the engine version/build, discovered classes and whether each
configured plugin loaded. GMAT can intentionally skip GUI plugins in headless mode;
missing/unloaded optional plugins do not prevent other engine features from working.

GMAT-Qt must include the Python API. In an otherwise configured Qt source build,
enable `-DGMAT_INCLUDE_API=ON -DAPI_GENERATE_PYTHON=ON` and install the matching
Python development headers and SWIG, then rebuild/install the API targets. A
desktop-only preview built with `GMAT_INCLUDE_API=OFF` cannot provide interactive
sessions. Keep each installation's bindings, libraries and plugins together.

`discover` searches a bounded set of directories and probes interpreters without
importing GMAT. Add repeated `--root` and `--python` arguments for custom locations.
It reports binding versions, architecture and missing setup. `init` writes only
compatible profiles, reports skipped installations and refuses to overwrite an
existing file unless `--overwrite` is explicit. `doctor` then loads each selected
engine in a private worker and checks API functions, startup, classes and plugins;
it does not execute a mission. On Windows the default configuration is under
`%LOCALAPPDATA%/gmat-mcp/config.toml`; data and desktop discovery records use
`%LOCALAPPDATA%/gmat-mcp` and its `desktops` subdirectory. Set explicit paths in
TOML to retain an existing custom or older data location.

### Windows setup

Extract NASA's `gmat-win-R2026a.zip` and install 64-bit CPython 3.11+ matching an
included `bin/gmatpy/_py3xx` directory. Install GMAT's required Microsoft Visual C++
runtime. Use the full Python executable path to avoid Windows Store aliases.
The server and workers may use different Python versions.

```powershell
uv sync --locked
uv run gmat-mcp discover --root 'C:\GMAT\R2026a' --root 'C:\src\GMAT-Qt\application' --python 'C:\Python312\python.exe'
uv run gmat-mcp init --root 'C:\GMAT\R2026a' --root 'C:\src\GMAT-Qt\application' --python 'C:\Python312\python.exe' --output profiles.local.toml
uv run gmat-mcp --config profiles.local.toml doctor
```

For an existing configured Qt source build, enable its Python API with matching
development headers/libraries and SWIG. For example, add these options to its
normal Visual Studio x64 configuration and rebuild:

```powershell
python integrations/prepare_qt_windows.py C:\src\GMAT-Qt --apply
cmake -S C:\src\GMAT-Qt -B C:\src\GMAT-Qt\build\windows -A x64 -DGMAT_INCLUDE_API=ON -DAPI_GENERATE_PYTHON=ON -DAPI_GENERATE_JAVA=OFF -DGMAT_PYTHON3_VERSIONS=3.12 -DGMAT_PYTHON312_ROOT_DIR=C:/Python312 -DSWIG_EXECUTABLE=C:/tools/swig/swig.exe
cmake --build C:\src\GMAT-Qt\build\windows --config Release
cmake --build C:\src\GMAT-Qt\build\windows --config Release --target deploy-qt
```

Keep the Qt build's existing Qt/CSPICE/OSG configuration options. An API-disabled
desktop preview needs rebuilding; copying another backend's bindings is unsupported.
The preparation script previews its diff without `--apply`; it prevents Windows SDK
`min`/`max` macros from breaking Yukon's standard-library calls in an API-enabled build.
Use the Qt checkout's runtime-deployment instructions for OSG and other non-Qt DLLs
as well; `deploy-qt` supplies Qt libraries and platform plugins for GUI attachment.
See `examples/profiles.windows.toml` for manually configured paths. Workers register
their selected DLL directories explicitly and validate interpreter/binding architecture.
Private workers use kill-on-close Windows Job Objects; attached desktops retain their
cooperative-stop and preservation contract. Bridge discovery records use a current-user
NTFS ACL and are rejected if readable by another account.

### macOS setup

Install the complete official R2026a distribution from NASA's
[release directory](https://sourceforge.net/projects/gmat/files/GMAT/GMAT-R2026a/).
The signed Mac disk image contains universal Intel/Apple Silicon binaries and
requires macOS 14.5 or later. Keep the external `bin`, `data` and plugin directories
with the app. Choose a Python 3.11+ interpreter matching an included binding version
and the architecture of the running process. The server may use a different Python.

```sh
uv sync --locked
uv run gmat-mcp discover --root /Applications/GMAT --root ~/src/GMAT-Qt/application --python /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12
uv run gmat-mcp init --root /Applications/GMAT --root ~/src/GMAT-Qt/application --python /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 --output profiles.local.toml
uv run gmat-mcp --config profiles.local.toml doctor
```

Discovery supports source layouts and API-enabled `.app` bundles. Universal bindings
are checked across all native modules against the selected interpreter's actual
architecture. An Intel interpreter under Rosetta remains Intel; automatic Rosetta
installation or translation is not performed. GMAT-Qt desktop previews with the API
disabled need rebuilding with SWIG and matching Python development headers. Build
all three `gmat_py3xx`, `station_py3xx` and `navigation_py3xx` targets as well as
`GmatQt`. See `examples/profiles.macos.toml` for paths.

For packaged Qt builds, a sidecar startup file is supported while relative paths
remain relative to `Contents/MacOS`. Workers resolve only the selected installation's
libraries, including `Contents/Frameworks` and `Contents/PlugIns`. No installed startup
file, app signature or system security setting is changed. If import fails, inspect
`otool -L` on that binding and install its matching dependencies; do not copy libraries
from another GMAT variant.

macOS uses `~/Library/Application Support/gmat-mcp` for retained data and private
bridge discovery records. Launch an opt-in Qt bridge with the executable inside
`Contents/MacOS` or the source build's `bin/GmatQt`. The native CI workflow downloads
NASA's signed image, builds a pinned API-enabled Qt revision with Homebrew dependencies,
and runs both engines through stdio and authenticated HTTP. It retains source/download
receipts, diagnostic output, JUnit results and the C++ bridge test log. GUI checks use
Qt's offscreen platform and do not qualify hardware rendering.
The official universal NASA image is cached between CI jobs and checked against its
recorded SHA-256 before use. Slow transfers switch to another official SourceForge
mirror; every downloaded image must match the same hash.

### Release bundle

Build local distributables with `uv run python tools/build_release.py`. The bundle
contains the wheel, source archive, hash-locked runtime requirements, examples, bridge
sources and a SHA-256 inventory. It includes no GMAT installation or bearer tokens.
Both GEO mission variants are included under `missions/`: use `_headless.script`
in isolated workers with MCP results/plots, and the companion script with native
OrbitView resources in an attached graphical desktop. See `examples/capabilities.md`.
After checking the separately distributed zip checksum and extracting the bundle:

```powershell
.\Install.ps1 -Python C:\Python312\python.exe -Destination C:\Tools\gmat-mcp-env
& C:\Tools\gmat-mcp-env\Scripts\gmat-mcp.exe discover --root C:\GMAT
```

On Linux and macOS use `python3 tools/build_release.py --uv /path/to/uv` to build and
`python3 install.py --destination /path/to/new/venv` inside the extracted bundle
to install. Installation creates a fresh environment and downloads only locked,
hash-verified runtime dependencies. Configure the MCP client with the resulting
`gmat-mcp` executable and your generated TOML file.

`tools/Qualify-Windows.ps1 -Config profiles.local.toml` runs the full suite with both
native profiles. Desktop checks launch disposable offscreen Qt processes. To run the
bridge's separate C++ regression test after installing its sources:

```powershell
cmake --build C:\src\GMAT-Qt\build\windows --config Release --target GmatQtMcpBridgeTests
ctest --test-dir C:\src\GMAT-Qt\build\windows -C Release -R '^QtGui.McpBridge$' --output-on-failure
```

Enable `GMAT_QT_BUILD_TESTS=ON` for that target and retain the runtime deployment above.
The manual
`native-windows.yml` workflow expects a dedicated self-hosted Windows x64 runner
labelled `gmat` with both engines configured through `GMAT_MCP_TEST_CONFIG`.

The server generates relocated startup files in its own data directory. It preserves
plugin order and repeated search paths and never edits the installed configuration.
Use the profile's `startup_file` to select a custom plugin configuration.
For Linux R2026a installations containing only versioned `.so.R2026a` libraries,
the server creates the missing loader aliases inside each session.

## Connect an MCP client

For stdio, configure the client to launch:

```json
{
  "command": "/absolute/path/to/GMAT-mcp/.venv/bin/gmat-mcp",
  "args": ["--config", "/absolute/path/to/profiles.local.toml", "serve"]
}
```

A Windows bundle installation uses the same arguments with Windows paths:

```json
{
  "command": "C:/Tools/gmat-mcp-env/Scripts/gmat-mcp.exe",
  "args": ["--config", "C:/GMAT/profiles.toml", "serve"]
}
```

For local Streamable HTTP:

```sh
export GMAT_MCP_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
uv run gmat-mcp --config profiles.local.toml serve --transport streamable-http
```

In PowerShell:

```powershell
$env:GMAT_MCP_TOKEN = & C:\Tools\gmat-mcp-env\Scripts\python.exe -c 'import secrets; print(secrets.token_urlsafe(32))'
& C:\Tools\gmat-mcp-env\Scripts\gmat-mcp.exe --config C:\GMAT\profiles.toml serve --transport streamable-http
```

Connect to `http://127.0.0.1:8000/mcp` with `Authorization: Bearer <token>`.
The service binds to loopback only and checks Host and Origin. It is a single-user
local service, not a remote or multi-tenant host. Installation profiles and GMAT
scripts are trusted local inputs: scripts may call installed Python/MATLAB plugins
and write explicit output paths. Worker isolation protects engine state and the
protocol; it is not an operating-system sandbox.

## Interactive workflow

1. `list_profiles`, then `create_session(profile_id)`.
2. Discover `list_classes` and `get_help`; use `create_object`, `get_object` and
   `set_field` to build the mission. Fields include native types, units and writability.
3. Use `append_command` with GMAT command descriptions, or `load_script` with text
   or a file. Branch commands need their corresponding End commands.
4. `validate_mission` initializes objects. It does not run commands or certify solver convergence.
5. `run_mission` returns immediately. Poll `get_run`, inspect configured/runtime
   objects, and retrieve `list_artifacts` / `read_artifact` and `get_logs`.
6. `export_script` saves reusable work; `close_session` releases the worker.

Example tool arguments for an API-built propagation:

```text
create_object(session_id, type="Spacecraft", name="Sat")
create_object(session_id, type="Propagator", name="Prop")
append_command(session_id, type="Propagate", description="Prop(Sat) {Sat.ElapsedSecs = 60}")
run_mission(session_id)
get_object(session_id, name="Sat", fields=["X", "Y", "Z", "Epoch"], view="runtime")
```

The `examples/` directory contains propagation, impulsive maneuver and targeting
scripts. Use `load_script(path=...)` to run them, and export scripts to exchange
missions with the Qt desktop. For live desktop control, enable the optional bridge
described below. Complete tool recipes are in [examples/capabilities.md](examples/capabilities.md).

Object reads explicitly distinguish `configured` (initial conditions) from `runtime`
(end-of-run or partial native-failure state). Results identify the model revision,
worker generation, run, and engine-provided epoch/frame context. Edits invalidate
runtime state. Units are GMAT's own units; no implicit frame/time conversion occurs.
Complex unsupported fields retain their text representation and can be edited via scripts.
Some GMAT fields have blank unit strings or unavailable writability metadata; these
are returned unchanged (unknown writability is `null`), rather than guessed.

Failed script imports preserve the previous session. `edit_mission` stages a batch
against an `expected_version` from `get_session`; dry runs return the candidate diff.
Successful transactions advance the version and support 20-step undo/redo. Command
branches move as whole subtrees; intersecting or ambiguous edits require separate
transactions or script replacement. Other failed low-level native mutations
can partially change the model; inspect fields and revalidate. Export uses GMAT's
serialization and may reformat comments. Low-level mutations clear transactional
undo/redo. Snapshots preserve Cartesian state and native A1 epoch precision while
restoring the selected display formats. Native status `1` means execution succeeded;
use `get_solver_results` to assess convergence independently.

## Results and visualization

`configure_results` adds visible managed ReportFile resources. Its typed specification
contains datasets with an ID, spacecraft, frame, origin body and optional custom
numeric GMAT parameters. Cartesian states and orbital elements require a frame;
orbital elements and ground tracks require a body. Collection follows native
published epochs, with final solver-pass output and no interpolation. Native GMAT
subscribers can suppress duplicate epochs or samples inside functions: collection
does not promise a sample at every mission command or on both sides of a burn.

`list_datasets` returns columns, native units, frames, time conventions, quality and
sample statistics. `query_results` pages by original sample index, optionally filtering
GMAT A1ModJulian epochs. Pages contain at most 1,000 rows and 256 KiB. GMAT modified
Julian epochs have a JD offset of 2,430,000; they are not conventional MJD values.
Invalid values become null with explicit quality markers; unexpected report headers
fail closed rather than assigning values to incorrect columns. Native reports remain
available. Partial results never turn a failed execution into a successful mission.

`plot_results` creates time histories, ground tracks and 3D trajectories, returns an
inline PNG preview, and retains PNG/SVG artifacts. Ground tracks split at longitude
wraps. Large plots retain per-bucket extrema and endpoints in at most 10,000 displayed
samples; CSV exports retain all data. Use `export_results` for complete CSV files and
`compare_runs` for per-installation metadata and a comparison CSV artifact.
Results configuration is embedded as a GMAT comment so exported/imported scripts
retain structured collection. Edits invalidate current runtime access but retain past
run datasets. Dataset metadata are also available as
`gmat://runs/{run_id}/datasets/{dataset_id}`.

## Live GMAT-Qt desktop

The bridge sources and an idempotent installer are included in `integrations/`.
The installer first prints a unified diff; use `--apply` to install it into a compatible
GMAT-Qt source checkout, then rebuild its `GmatQt` target. The local checkout used
for qualification already has this integration installed and built.

```sh
python integrations/install_qt_bridge.py /path/to/GMAT-Qt
python integrations/install_qt_bridge.py /path/to/GMAT-Qt --apply
cmake --build /path/to/GMAT-Qt/build/linux-gui --target GmatQt
/path/to/GMAT-Qt/application/bin/GmatQt --mcp-bridge /path/to/mission.script
```

`list_desktops` discovers opted-in instances. `attach_desktop(desktop_id, profile_id)`
binds a session to that process and document, using its matching Qt installation.
The bridge binds loopback, authenticates every message with a private random token,
limits messages to 1 MiB, and permits one controlling connection. Tokens stay out of
tool responses and logs. `[server].desktop_discovery_dir` and Qt's
`--mcp-discovery-dir` can select the same explicit directory; the default is the
platform's user data directory for `gmat-mcp`, under `desktops/`.

Use `get_session` to inspect editor/build state. Desktop mutations require a current
version: use `edit_mission`, `configure_results(expected_version=...)` or
`load_script(expected_version=...)`. Unbuilt editor text and pending panels are
protected; `build_desktop` explicitly builds current editor text. Desktop edits support
parse validation; object initialization is checked on GUI Run. Worker sessions also
support independent initialization validation. Operations run on Qt's owning thread,
and status/logs remain accessible during execution. `pause_run`/`resume_run` use Qt's
native controls. A document switch requires explicit reattachment.

MCP-started runs appear in the GUI's existing views. GUI-started runs become visible
through `get_session` while attached, including their available artifacts and solver
listeners. `detach_desktop` never closes the application. Cancellation requests a
cooperative stop; an unresponsive GUI is marked unavailable with completion unknown.
Neither cancellation nor server shutdown force-kills GMAT-Qt. On disconnect, only an
MCP-owned run receives a stop request; a GUI-owned run is preserved.
After cancelling an MCP-owned desktop run, detach the old session and attach again
to continue controlling the preserved window.

## Saved projects and trade studies

`save_project` retains a mission, collection settings, profile/build identity and
explicitly listed relative input files with checksums. Installed GMAT data stay with
the installation. `open_project` opens a fresh session and reports profile differences;
it checks saved input/mission integrity. Resolve any other project dependencies before
running. `list_projects` and `list_runs` survive server restarts; live sessions do not.

`start_study` freezes a baseline and returns immediately. It accepts explicit cases,
parameter grids or seeded differential evolution over bounded real/integer fields.
Metrics select runtime fields or collected columns and apply declarative reductions,
differences, absolute differences or angular distance in degrees. There is no Python
evaluation interface. One objective may be minimized/maximized with numeric constraints.
Failed cases and known nonconvergence are infeasible; unknown solver convergence
requires explicit opt-in. The best feasible script is retained without applying it.

Default study limits are 100 evaluations, one hour, seed 0 and at most four concurrent
workers, additionally constrained by available session slots. The baseline session
uses a slot; close other idle sessions when more capacity is needed. Each case uses a
fresh worker and the existing per-run timeout. Initial population evaluations count
against the budget; polishing is disabled. `get_study` distinguishes budget exhaustion
from optimizer convergence. `list_study_cases`, `cancel_study` and `compare_runs`
provide outcomes, cleanup and comparison exports. Interrupted studies remain readable
and are never replayed automatically. Cross-variant studies preserve separate engine
identity and do not assume numerical equivalence.

## Lifecycle and artifacts

The default limits are four sessions, 60 seconds per ordinary operation, and 30 minutes
per run. Set `max_sessions`, `operation_timeout`, `run_timeout`, and `data_dir` under
`[server]`. Session native operations are serialized; busy sessions reject competing
operations while status/log/artifact reads remain available. Cancellation and timeout
terminate the worker and require `reset_session` or `load_script`. The server never
replays a failed run. Reset discards unsaved state and invalidates command IDs.

By default artifacts live in the platform's user data directory for `gmat-mcp`.
Each run retains its mission snapshot, effective startup, engine/library hashes,
native result, summary, logs and generated output. Artifact reads are bounded to
64 KiB and use byte offsets; binary data is base64. Explicit script output paths
retain GMAT semantics and may point outside the run directory.
Run summaries returned by tools are limited to 65,536 characters; `summary.txt`
retains the complete summary when `summary_truncated` is true.

Live sessions are not restored after server restart. Saved projects and run manifests
are indexed in a versioned SQLite catalog; legacy run directories are imported.
Interrupted queued/running manifests are marked failed without replay. Remove old artifact
directories manually when no longer needed. Resources mirror profiles, sessions,
runs and individual listed artifacts under `gmat://` URIs.

## Development and verification

```sh
uv sync --locked
uv run ruff check .
uv run pytest -m 'not native'
GMAT_MCP_TEST_CONFIG="$PWD/profiles.local.toml" uv run pytest -m native
```

Portable protocol/lifecycle tests use a small fake engine. Native integration tests
use real installations and MCP clients over stdio and HTTP, with direct API baselines.
See `VALIDATION.md` for the recorded local qualification and platform limits.
The GitHub Actions workflow runs portable tests and package builds on Linux, macOS
and Windows using Python 3.11 and 3.14. Native GMAT qualification remains a separate
run with explicitly configured installations.
