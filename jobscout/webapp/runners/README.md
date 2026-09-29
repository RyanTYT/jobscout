# webapp/runners/ — background orchestration

The two long-running jobs the dashboard launches. Both are plain modules
(unit-testable without HTTP); the routers call in and the partials poll.

## agent_runner.py — the hunt lifecycle

`launch(focus)` runs a full hunt in a background thread: focus="" chains
`run --daily` (the sweep that fills the inbox) THEN `agent` (the LLM
tool loop); focus="profile" runs the agent alone with a profile-focused
brief. Popen (killable) — `cancel()` terminates mid-run, partial output
kept and labelled "run cancelled by you". Owns the run state (out/error/
focus), strips ANSI from output, extracts the last error line for the
failure callout, and drains sidecar apply-run events into `apply_runs`
(`refresh_runs`). The run-status partial polls every 3s while running.

## apply.py — the apply launcher

`launch_apply(conn, packet_ids)` splits the selection: **automated**
(posting URL matches a JobPilot filler — greenhouse/lever/ashby/linkedin)
→ one headed sidecar run with pauseOnUncertainty + tiled window hints;
**assisted** (no filler) → tiled browser windows via the OS `open`.
`tile_grid()` computes screen rects from Finder bounds. Tracks runs in
the `apply_runs` table; the sidecar singleton (`get_sidecar`) is shared.

**Tests:** `tests/test_apply.py`, `tests/test_routes_discovery.py`
(launch/cancel/focus with a fake Popen — never a real subprocess).
