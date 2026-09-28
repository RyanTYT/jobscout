# jobscout desktop (Tauri)

A thin desktop shell around the jobscout Python backend. The Rust app
picks a free local port, spawns the backend, waits for it to listen,
opens the webview window on it, and kills the backend when the app
quits. Everything else — the dashboard, packets, the apply launcher —
is the FastAPI app, unchanged.

```
desktop/
  dist/index.html          placeholder (satisfies frontendDist; never shown)
  freeze/entry.py          PyInstaller entry for the backend binary
  icon.png                 brand icon source (radar motif on the gradient)
  binaries/                freeze output (gitignored) → bundled as resources
  src-tauri/
    src/backend.rs         child-process lifecycle + first-run bootstrap
    src/lib.rs             app wiring, window, file logger, exit cleanup
    tauri.conf.json        bundle config (backend → Resources/backend/)
```

## Dev — run the app from the repo checkout

```sh
cd desktop
npm install            # once: @tauri-apps/cli
npm run dev            # cargo build + run; spawns ../.venv/bin/jobscout
```

Backend resolution order (backend.rs): `JOBSCOUT_BIN` env → frozen
binary in resources → `.venv/bin/jobscout` found by walking up from the
exe/CWD. In dev the runtime stays repo-relative (data/, config/,
master_resume/, applications/ — the checkout you already use).

## Release — build the .app / .dmg

```sh
cd desktop
npm run build:release  # freeze backend + tauri build (~2 min)
```

which is:

1. `../.venv/bin/pyinstaller … freeze/entry.py` → `binaries/jobscout-server/`
   (one-dir; webapp templates/static + `jobscout/defaults/` bundled as data)
2. `npx tauri build` →
   `src-tauri/target/release/bundle/macos/jobscout.app` (~52 MB)
   `src-tauri/target/release/bundle/dmg/jobscout_0.1.0_aarch64.dmg`

The packaged app:

- spawns `Resources/backend/jobscout-server serve --port <free>`
- sets `JOBSCOUT_HOME` → `~/Library/Application Support/com.jobscout.desktop`
  (all runtime state relocates there: db, config, resume, packets)
- runs `jobscout init-home` first (idempotent): seeds `config/settings.yaml`
  + `master_resume/resume.yaml` from `jobscout/defaults/` (a snapshot of the
  repo's files — refresh it when the defaults change) and initialises the DB
- logs to `<app-data>/logs/desktop.log` and stderr

Deploying the .dmg to another Mac: no signing needed for personal use
(right-click → Open the first time). For distribution beyond your own
machines you'd add a Developer ID + notarization.

## JobPilot (optional, not bundled yet)

The automated apply path needs the JobPilot sidecar. In dev it resolves
`../JobPilot/scraper/dist/index.js` automatically. To bundle it later:
ship `scraper/dist/` (+ node + playwright browsers) as another resource
and set `JOBSCOUT_SIDECAR_BIN` in backend.rs's frozen branch — the
Python side already honors that env var. Until then the packaged app
degrades gracefully: every apply URL opens assisted (tiled browser).
