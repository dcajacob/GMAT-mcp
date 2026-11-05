# Capability recipes

These are MCP tool arguments, with returned IDs substituted in later calls. Start
with `list_profiles`, then `create_session` using either configured installation.

## Propagation and collected results

Load `examples/propagation.script`. Configure collection before running:

```json
{"session_id":"SESSION","spec":{"datasets":[{"id":"orbit","spacecraft":"Sat","frame":"EarthMJ2000Eq","body":"Earth","groundtrack":true}]}}
```

Call `run_mission`, poll `get_run`, then use `list_datasets`. Inspect the reported
units and epoch conventions. `query_results` can select columns:

```json
{"run_id":"RUN","dataset_id":"orbit","columns":["Sat.ElapsedSecs","Sat.EarthMJ2000Eq.X","Sat.EarthMJ2000Eq.Y"],"limit":200,"offset":0}
```

Continue at the returned `next_offset` while `has_more` is true. For charts, call
`plot_results` with the same run/dataset and `kind` equal to `ground_track`,
`orbit_3d`, or `time_history`. Time histories may specify a list of columns sharing
units, such as `["Sat.Earth.SMA"]`. `export_results` returns a full CSV artifact.

For the three-day Americas GEO example, load
`missions/AmericasGEO_15deg_90W_headless.script` and use spacecraft `AmericasGEO`.
The dataset statistics contain longitude bounds without downloading the complete
history. Use the companion script without `_headless` in a graphical desktop.

## Atomic edits, previews and history

Read `model_version` from `get_session`. Call `edit_mission`:

```json
{"session_id":"SESSION","expected_version":"VERSION","dry_run":true,"validation":"initialize","changes":[{"op":"set_field","name":"Sat","field":"DryMass","value":500},{"op":"set_field","name":"Sat","field":"X","value":7100}]}
```

The dry run returns a diff and validation results without committing. Submit the
same request with `dry_run:false` to commit. `undo_edit` and `redo_edit` require the
current version and return a new one. Desktop edits use `validation:"parse"`.

Use `list_commands` for command IDs. A replacement is
`{"op":"replace_command","command_id":"COMMAND","text":"Propagate Prop(Sat) {Sat.ElapsedSecs = 600};"}`.
Insertion adds `position:"before"` or `"after"`. Movement uses
`{"op":"move_command","command_id":"COMMAND","target_id":"DESTINATION","position":"after"}`.
Branches move with their bodies. Overlapping edits are rejected.

## Targeting diagnostics and saved projects

Load `examples/targeting.script`, validate and run, then call `get_solver_results`.
The differential corrector targets 8,000 km apogee radius with a 0.01 km tolerance.
Check convergence and reported residuals independently of the run status. Reducing
`DC.MaximumIterations` to 1 provides a nonconvergence example.

Save with `save_project(session_id, name="Targeting", inputs=[])`. For a mission
using a local gravity file or include, list those relative files explicitly in
`inputs`. Use `list_projects` and `open_project` after a restart. Select a different
`profile_id` explicitly to exchange the saved mission between installations;
review the engine-difference warning and requalify the result.

## Live desktop

Launch a bridge-enabled GMAT-Qt with `--mcp-bridge`, then use `list_desktops` and
`attach_desktop`. Read `get_session` and its `desktop` state. Edits update the GUI
through its native controllers; Run updates its existing graphical views.
Use `build_desktop` to build GUI editor changes explicitly, and refresh the version
before an MCP edit. `pause_run`, `resume_run` and `cancel_run` use native controls.
Finish with `detach_desktop`; the window remains open.

## GEO parameter sweep and longitude search

Load the headless Americas mission and configure dataset `geo` for `AmericasGEO`.
Call `start_study` with:

```json
{"session_id":"SESSION","spec":{"mode":"grid","profiles":["nasa-r2026a","gmat-qt"],"grid":{"AmericasGEO.INC":[14,15,16],"AmericasGEO.ECC":[0.02,0.023716819497842816,0.025]},"metrics":[{"name":"longitude_excursion","source":"dataset","dataset_id":"geo","column":"AmericasGEO.Earth.Longitude","transform":"angular_distance","reference":-90,"reduce":"maximum"}]}}
```

For orientation search, select one profile and change `mode` to `optimize`, replace
`grid` with `variables:[{"field":"AmericasGEO.RAAN","lower":218.94,"upper":226.94}]`,
and add `objective:{"metric":"longitude_excursion","direction":"minimize"}`.
Poll `get_study`, page `list_study_cases`, and compare selected case run IDs with
`compare_runs`. The baseline remains unchanged. Budget exhaustion means the best
sampled feasible case was retained; it does not claim optimizer convergence.

## Constrained maneuver optimization

Load `examples/maneuver.script`. Minimize the burn while requiring final SMA of at
least 7,500 km:

```json
{"session_id":"SESSION","spec":{"mode":"optimize","profiles":["nasa-r2026a"],"variables":[{"field":"Burn.Element1","lower":0.1,"upper":0.4}],"metrics":[{"name":"dv","object":"Burn","field":"Element1"},{"name":"sma","object":"Sat","field":"SMA"}],"objective":{"metric":"dv"},"constraints":[{"metric":"sma","op":">=","value":7500}],"max_evaluations":100,"seed":0}}
```

Values use native GMAT units: this burn component is km/s and SMA is km. Inspect the
winning case's feasibility, native diagnostics and artifacts. Its `run_id` provides
the `mission.script` artifact for explicit loading into a new session or the desktop.
Nothing applies the winning case automatically.
