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

## Copying to another Mac + building from source there

The complete source is the git-tracked tree — 188 files, ~2.3 MB:

| area | files | what it is |
|---|---|---|
| `jobscout/` | 73 | the Python application (core, sources, scoring, packets, agent, webapp, defaults) |
| `desktop/` | 68 | the Tauri shell (Rust, config, icons, freeze entry, build script) |
| `tests/` | 9 | pytest suite |
| `config/` | 4 | settings.yaml, profile.yaml (hunting profile), watchlist.yaml, models.yaml |
| `master_resume/` | 3 | the single source of truth |
| `applications/` | 12 | generated packets |
| `pyproject.toml`, `bootstrap.sh`, … | rest | packaging + docs |

The one-command export (exactly those files, nothing generated):

```sh
git archive --format=tar.gz -o /tmp/jobscout-source.tar.gz HEAD
```

Copy `jobscout-source.tar.gz` over (AirDrop/rsync/scp), plus — only if
you want continuity — the untracked runtime state: `data/jobscout.db`
(your postings/signals), and `.env` (LLM key; NEVER commit it).

On the other Mac (toolchain: [rustup], Node ≥ 18, Python ≥ 3.11):

```sh
tar xzf jobscout-source.tar.gz -C ~/Personal\ Project/   # or wherever
cd jobscout
python3 -m venv .venv && .venv/bin/pip install -e ".[web,dev]" pyinstaller
.venv/bin/jobscout db init && .venv/bin/jobscout init-home   # if JOBSCOUT_HOME used
cd desktop
npm install
npm run dev              # dev: repo backend + window
npm run build:release    # or: jobscout.app + jobscout_*.dmg (~2 min)
```

Quickest alternative — skip all of the above: just AirDrop the built
`desktop/src-tauri/target/release/bundle/dmg/jobscout_0.1.0_aarch64.dmg`
(25 MB, self-contained, seeds its own runtime on first launch).

## JobPilot (optional, not bundled yet)

The automated apply path needs the JobPilot sidecar. In dev it resolves
`../JobPilot/scraper/dist/index.js` automatically. To bundle it later:
ship `scraper/dist/` (+ node + playwright browsers) as another resource
and set `JOBSCOUT_SIDECAR_BIN` in backend.rs's frozen branch — the
Python side already honors that env var. Until then the packaged app
degrades gracefully: every apply URL opens assisted (tiled browser).
