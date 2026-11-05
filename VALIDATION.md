# GMAT MCP 0.3.0 qualification

Validation date: November 4, 2026 (America/Denver).

## Results

The Windows milestone adds installation discovery/profile generation, interpreter and
PE/ELF architecture checks, Windows Job Object ownership, private NTFS bridge records,
Windows native test launchers, and a checksummed installation bundle. macOS qualification work is in progress; no native Mac pass is claimed below.

| System | Server Python | Suite | Result | Seconds |
| --- | --- | --- | --- | ---: |
| Linux x86-64 | 3.11.18 | Full portable + native | 49 passed, 2 skipped | 185.56 |
| Linux x86-64 | 3.14.4 | Full portable + native | 49 passed, 2 skipped | 186.69 |
| Windows 11 x86-64 | 3.14.10 | Full portable + native | 49 passed, 2 skipped | 465.35 |
| Windows 11 x86-64 | 3.11.18 | Portable | 36 passed, 2 skipped | 48.36 |

The full suites include both engines over stdio and authenticated Streamable HTTP.
Linux skips Windows Job/NTFS tests; Windows skips POSIX permissions/Linux loader aliases.
Native `QtGui.McpBridge` CTest passed separately on Linux and Windows (2.75 seconds
for the Windows test). Ruff lint/format and the dependency lock check passed.

Reports are retained locally under `test-results/windows-milestone-linux-py311.xml`,
`test-results/windows-milestone-linux-py314.xml`, and `test-results/windows/`.
The latter includes native Windows XML, portable Python 3.11/3.14 reports, engine
`doctor.json` and the separate Qt CTest log.

The wheel, source archive and installation bundle are built by `tools/build_release.py`.
The bundle includes a file inventory and a separate zip checksum. A clean hash-locked
installation passed on Linux; the PowerShell installer passed on Windows in a path
containing spaces. Final wheel smoke results are retained beside the test reports.

## Native installations

Linux server versions use independent Python 3.14.4 native workers. Windows uses a
Python 3.14.10 server with Python 3.12.0 workers. Both exercise the cross-interpreter
boundary. Workers use only the standard library.

- NASA R2026a Linux x86-64: March 30, 2026 engine build; 557 discovered classes.
- GMAT-Qt Linux development installation: November 4, 2026 engine build; 570 classes.
- The GMAT-Qt bridge was installed into the local source checkout, rebuilt, and tested
  with disposable windows on private software-rendered X11 displays. Existing user
  desktop instances were not controlled or terminated.
- Windows 11 x86-64, kernel build 26300, qualified in the existing local VM.
- NASA R2026a Windows x86-64: March 26, 2026 build; 562 classes.
- GMAT-Qt Windows x86-64: November 4, 2026 build; 563 classes. An isolated source
  copy was built with Visual Studio 2022, SWIG 4.5.1 and the core/station/navigation
  Python 3.12 API modules. Qt 6.10.2 and existing matching CSPICE/OSG dependencies
  were reused. User installations and running desktop sessions were preserved.
- The Qt Windows API build required parenthesized Yukon `std::min`/`std::max` calls
  to avoid Windows SDK macro expansion; `integrations/prepare_qt_windows.py` previews
  or applies that change. Native Qt CTest uses the repository's Windows DLL launcher
  and the deployed platform-plugin directory.
- MCP SDK 2.3.0; plotting/optimization dependencies and Python-version markers are
  recorded in `uv.lock`.

## Qualified workflows

Actual MCP clients exercised **stdio and authenticated Streamable HTTP** against
**both installations on each qualified operating system**. Original propagation, maneuver and differential-corrector
workflows remain covered. Independent direct API subprocesses use the same installation,
startup/data and mission input. State comparisons use relative tolerance 1e-12 and
absolute tolerance 1e-9; epoch and original summaries are also checked.

New acceptance coverage includes:

- Automatic native histories without propagation rewrites; typed columns, native
  units, frame/origin metadata, native A1 epoch conventions, statistics, SQLite paging,
  CSV export, inline PNG previews and PNG/SVG artifacts.
- Three-day Americas GEO propagation and ground tracks through both transports and
  engines, including a direct-run comparison establishing unchanged terminal state.
- Staged field/command edits, dry-run diffs, stale-version rejection, failed-edit
  preservation, whole-branch editing, undo/redo, script round trips retaining the
  embedded collection specification, and project save/open.
- Native solver-clone status rather than the unused configured solver object;
  differential-corrector final variables, goals, tolerances, residuals and iteration
  counts parsed from version-qualified reports; positive and iteration-limit cases.
- Live Qt attachment, GUI-visible edits, native orbit/ground-track views, collection,
  busy rejection, pause/resume, logs, cooperative cancellation and safe detachment,
  through both MCP transports.
- The separate C++ Qt regression test covers invalid authentication, GUI editor
  versions, stale edits, unbuilt text, pending panels, explicit builds, document
  switches and safe disconnect. Portable tests cover an unresponsive bridge and
  preservation of the application contract.
- GEO inclination/eccentricity sweeps across installations, longitude-confinement
  optimization, and maneuver optimization subject to a final-SMA constraint, through
  both transports. Additional checks cover fixed seeds, evaluation budgets, immediate
  cancellation, baseline preservation, comparisons and restart-readable records.
- Partial/truncated reports, repeated epochs, time reversals, invalid values, bounded
  queries and plot downsampling; catalog recovery without replay and preservation of
  another live server's records.

Windows-specific regression checks cover process-tree termination via Job Objects,
current-user NTFS ACLs and rejection of broad discovery ACLs, missing interpreters,
PE architecture detection, locked installation into a path containing spaces, and
UTF-8 artifact byte counts. GMAT's Windows path detection depends on `OS=Windows_NT`;
workers supply it when a stdio client's minimal environment omits it. Provenance
hashes are computed from saved startup bytes, including native newline conventions.

Original portable checks still cover worker crashes/timeouts, busy sessions, missing
bindings/interpreters/plugins, native stdout isolation, HTTP authentication and
Host/Origin checks, bounded artifacts, symlink exclusion, startup relocation, loader
aliases and shutdown cleanup. Native tests verify installed startup hashes are unchanged.

## Reproduce

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
GMAT_MCP_TEST_CONFIG="$PWD/profiles.local.toml" uv run pytest -q
uv build
```

Without `GMAT_MCP_TEST_CONFIG`, native tests are skipped. Configure both installations
with `examples/profiles.toml` or `examples/profiles.windows.toml`. Linux desktop tests
additionally require Xvfb and a bridge-enabled `GmatQt` binary. Windows desktop tests
launch disposable offscreen instances of `GmatQt.exe` with its Qt/OSG runtime deployed.
The Qt checkout includes a registered regression test:

```sh
cmake --build /path/to/GMAT-Qt/build/linux-gui --target GmatQt GmatQtMcpBridgeTests
ctest --test-dir /path/to/GMAT-Qt/build/linux-gui -R '^QtGui.McpBridge$' --output-on-failure
```

## Limits

The portable CI matrix targets Linux, Windows, Apple Silicon and Intel macOS with Python 3.11 and 3.14.
`native-windows.yml` provides manual qualification on a dedicated configured runner;
`release.yml` builds an installation candidate after the portable matrix passes.
The public hosted workflows are now running; see the macOS section below for qualification status.

Windows bridge qualification uses Qt's offscreen platform. It verifies editing,
execution, results, pause/resume, cooperative cancellation and application preservation;
it does not establish hardware-rendering quality on an interactive Windows desktop.
This qualification establishes bounded integration behavior, not all GMAT science,
plugins or GUI functionality.

Native published samples do not promise a fixed cadence or every discontinuity.
Isolated GEO checks use the `_headless.script` example; native OrbitView resources
run through an attached desktop using its graphical companion script.
Desktop transactions use parse validation; GUI Run checks initialization. Cancellation
of an MCP-owned desktop run preserves the window but requires reattachment for further
control. Native worker sessions retain their reset/reload requirement after cancellation.

Optional Python/MATLAB callbacks, navigation/estimation, external force models and full
plugin parity remain outside the qualified workflows. Fixed-cadence sampling, remote
hosting, automatic session recovery, a separate interactive results viewer and
multi-objective optimization remain deferred. Optimizer budget exhaustion is reported
separately from convergence, and no winning case is applied automatically.

## macOS qualification in progress

The public repository is https://github.com/dcajacob/GMAT-mcp. The native Mac workflow
uses separate `macos-15` (Apple Silicon) and `macos-15-intel` runners. It downloads
the official signed R2026a image and builds the public GMAT-Qt source at
`e0aa985a37769ad25d17d96275c4a0d2410f1a0e`, applying this repository's bridge integration.
Each runner exercises both engines through stdio and authenticated Streamable HTTP.
Native results will be recorded after those jobs complete.

Portable regression coverage now includes thin and universal Mach-O headers,
FAT32/FAT64 in both byte orders, truncated/mismatched slices, mixed-module
architecture rejection, source/app-bundle discovery, sidecar startup relocation,
NASA API symlinks, and selected-installation-only DYLD paths. These synthetic
fixtures establish loader/setup behavior, not native mission correctness.

Local Linux regression of the macOS implementation passed 62 tests with two Windows-only
skips in 167.37 seconds. The separate POSIX descendant-cleanup regression also passed.
The updated Qt C++ bridge regression passed in 0.70 seconds. A clean, hash-locked
wheel installation propagated the headless Americas GEO mission successfully on both
local engines; results are retained in `test-results/macos-linux-wheel-smoke.json`.

The initial public [portable workflow](https://github.com/dcajacob/GMAT-mcp/actions/runs/41971514934)
passed all eight jobs: Linux, Windows, Apple Silicon and Intel macOS, each with server
Python 3.11 and 3.14. Mac jobs reported 48 passed and three platform-specific skips.
The subsequent POSIX process-group regression raises the portable test count by one.
