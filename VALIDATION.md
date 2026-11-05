# GMAT MCP 0.4.0rc1 qualification

Validation date: November 4, 2026 (America/Denver). GitHub logs and native build
dates use UTC and can therefore show November 5.

This milestone adds macOS installation discovery, thin/universal Mach-O checks,
app-bundle paths, selected-installation library loading and native Mac CI. GMAT
and its data remain separate installations. Both NASA R2026a and an API-enabled
GMAT-Qt build are exercised independently.

## Current results

Native Mac qualification uses server Python 3.14 and Python 3.12.10 workers.
The clean-wheel environments use Python 3.14.10 on Apple Silicon and 3.14.7 on Intel.
The full suites exercise **both engines over stdio and authenticated Streamable
HTTP**, including the live Qt bridge. Each architecture also passes the separate
C++ bridge regression and runs the headless Americas GEO mission from a clean,
non-editable wheel installation with hash-locked dependencies.

| System | Suite | Result | Seconds |
| --- | --- | --- | ---: |
| macOS 15.8.2, Apple Silicon | Full portable + native | 65 passed, 3 skipped | 253.26 |
| macOS 15.7.9, Intel | Full portable + native | 65 passed, 3 skipped | 416.63 |
| Linux x86-64, Python 3.14.4 | Runtime + native regression | 63 passed, 2 skipped | 166.22 |
| Linux x86-64, Python 3.14.4 | Final portable suite, including CI asset regressions | 53 passed, 2 skipped | 11.38 |
| Linux x86-64, Python 3.11.18 | Portable suite before CI asset regressions | 50 passed, 2 skipped | 10.10 |

The Linux runtime report predates the three added CI download tests; these pass
in the final portable suite. The final hosted portable matrix passes all **eight**
jobs: Linux, Windows, Apple Silicon and Intel macOS, each on Python 3.11 and 3.14.
Mac skips cover Windows Job Objects, Windows NTFS ACLs and Linux loader aliases.

The native Qt C++ test passes in 1.32 seconds on Apple Silicon and
1.19 seconds on Intel. Clean-wheel GEO runs succeed on both
engines on each architecture; the smoke test also checks the saved mission and
startup bytes against their recorded provenance hashes.

- [Final native Mac workflow](https://github.com/dcajacob/GMAT-mcp/actions/runs/41976494867)
- [Final portable workflow](https://github.com/dcajacob/GMAT-mcp/actions/runs/41976495066)
- Qualified runtime source: `7f2fd7bbd250600c6d84a0259554332a3fc032d7`.

Workflow artifacts retain JUnit reports, engine diagnostics, build/download
receipts, the native bridge log and the installed-wheel mission results. Local
copies are under `test-results/macos-final-arm64/` and
`test-results/macos-final-intel/`. Linux reports include
`test-results/macos-rc1-linux-native.xml` and
`test-results/macos-download-portable.xml`.

## Native installations and provenance

- NASA's signed universal R2026a Mac image reports an engine build of March 26,
  2026, with 567 classes. Its external API symlinks resolve into the signed app's
  `Contents/Frameworks`; the installed files and signatures remain untouched.
- NASA image SHA-256:
  `8a81d22ce2d39504eadc875d71494321adb49f60da92d266fdbe660de0f771d2`.
  CI checks cached and freshly downloaded copies against this hash, switches
  away from slow official mirrors and retains only a verified image for reuse.
- GMAT-Qt is built from the public source at
  `e0aa985a37769ad25d17d96275c4a0d2410f1a0e`, with this repository's bridge
  integration applied. It reports R2026a, November 5, 2026, with 561 classes.
  Each runner builds its own architecture, including all three core, station
  and navigation Python API modules. No Rosetta translation is requested.
- Qt builds use a macOS 15.0 deployment target, native JPL CSPICE, Qt 6,
  OpenSceneGraph and Xerces. Receipts record the actual Homebrew package
  versions, CMake options, source revision and downloaded archive hashes.
- Linux regression uses the existing NASA R2026a installation (557 classes)
  and local Qt development installation (570 classes), with Python 3.14.4
  native workers.
- Isolated-worker runs record engine library hashes and effective startup files.
  Cross-variant exchange tests retain separate engine identities and do not
  assume numerical equivalence.

## Covered behavior

Independent direct API subprocesses use the **same installation, mission and
data** as their MCP counterparts. Terminal Cartesian states use relative
tolerance `1e-12` and absolute tolerance `1e-9`; native epoch values and mission
summaries are also compared. Adding collection resources preserves the terminal
state within those tolerances.

The suites cover propagation, impulsive maneuvers, converged targeting,
iteration-limit targeting and the three-day Americas GEO analysis. They exercise
native histories, units/frame/time metadata, statistics, SQLite paging, CSV
exports, inline PNG previews and full PNG/SVG plots. Native samples retain their
order, repeated epochs and time reversals. Partial/truncated reports and invalid
values have separate dataset status.

Editing checks cover staged imports and transactions, field validation, command
branches, dry-run diffs, stale versions, failed-edit preservation, undo/redo,
script round trips, project persistence and historical results after edits.
Solver checks use native runtime clones and version-qualified reports, with
explicit unavailable fields rather than inferred convergence or iteration history.

Trade studies cover inclination/eccentricity sweeps, longitude-confinement
optimization and a maneuver objective with a final-SMA constraint. Fixed seeds,
budgets, cancellation, baseline preservation, comparisons and restart-readable
records are also checked. Budget exhaustion remains distinct from convergence.

Live Qt tests cover GUI-visible edits, native view receivers, collection, busy
rejection, pause/resume, logs, cooperative cancellation and safe detachment,
through both transports. The separate C++ test covers authentication, stale GUI revisions, unbuilt text, pending panels, explicit builds,
document switches and safe disconnection. An unresponsive desktop remains alive
with an unknown completion state.

Portable regressions include Mach-O FAT32/FAT64 in both byte orders, malformed
slices, mixed-module architecture rejection, app/sidecar startup relocation,
NASA API symlinks and isolation from inherited dyld overrides. POSIX process-group
cleanup reaches descendants. Other checks cover worker crashes/timeouts, bounded
artifacts, symlink exclusion, native stdout isolation, HTTP authentication and
Host/Origin validation, catalog recovery without replay, and installed startup
hash preservation. CI asset tests reject incorrect hashes, preserve existing
files after failed downloads and verify cache reuse and mirror fallback.

## Reproduce

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
GMAT_MCP_TEST_CONFIG="$PWD/profiles.local.toml" uv run pytest -q
uv run python tools/build_release.py
```

Configure both installations using `examples/profiles.macos.toml` or the Linux/
Windows examples. Without `GMAT_MCP_TEST_CONFIG`, native tests are skipped. The
hosted `native-macos.yml` workflow prepares both engines from their recorded
sources; it can also be started manually. Linux desktop tests require Xvfb.
Mac and Windows desktop qualification uses disposable offscreen Qt instances.
See `README.md` for the API/bridge build and installation commands.

## Earlier Windows qualification

The existing Windows 0.3.0 qualification passed 49 tests with two platform skips
on Windows 11 x86-64 using server Python 3.14.10 and Python 3.12.0 workers. Its
portable Python 3.11 suite passed 36 tests with two skips. Both native backends
ran over both transports; the separate Qt bridge test passed in 2.75 seconds.
An installed-wheel GEO smoke and the PowerShell bundle installer also passed.
Those reports remain under `test-results/windows/`.

That Windows baseline covers Job Object process-tree cleanup, current-user NTFS
discovery ACLs, PE architecture checks, UTF-8 byte counts and installation into a
path containing spaces. Workers restore `OS=Windows_NT` when an MCP client's
minimal environment omits it. Native 0.4.0 Windows requalification is not claimed;
the final portable Windows matrix passes on Python 3.11 and 3.14.

## Limits

Native Mac qualification covers macOS 15 on Apple Silicon and Intel. The Qt
installation is an API-enabled source build; the official NASA distribution
exercises real app-bundle API symlinks. Additional Qt app-bundle layouts are
covered by portable fixtures, rather than a redistributed signed Qt package.

GUI checks use Qt's offscreen platform and establish editing, execution, result
delivery and application preservation, not interactive hardware-rendering quality.
Native isolated sessions use the headless GEO script; OrbitView resources use its
graphical companion in an attached desktop. Published native samples do not promise
a fixed cadence or every discontinuity.

Successful parsing/initialization does not imply successful execution or solver
convergence. Isolated worker cancellation, timeout or crashes require explicit
reset/reload. Desktop cancellation preserves the application and requires
reattachment for further control. No interrupted mission is automatically replayed.

Optional callbacks, navigation/estimation workflows, external force models and
full plugin parity remain outside this qualification. Fixed-cadence sampling,
remote hosting, automatic recovery, a separate interactive results viewer and
multi-objective optimization remain deferred.
