# jobscout

Cost-effective daily AI job-hunt pipeline. An agent-driven discovery layer (switchable),
a free deterministic collection layer, cheap cached LLM scoring, and grounded
application packets built entirely from a single master resume.

**Read [PLAN.md](PLAN.md) first** — it is the single source of truth for design
and progress. [AGENTS.md](AGENTS.md) covers conventions for coding agents.
Each package directory carries its own `README.md` explaining how it works.

## The loop (what the system does)

```
watchlist (companies you care about, with ATS board tokens)
   │
   ▼
sources/postings  ── polls ATS boards + crawls dark-pool careers pages,
                    searches job sites (MyCareersFuture public API +
                    the JobPilot sidecar: LinkedIn, Indeed, YC, Wellfound)
                                                          ──▶ postings
sources/discovery ── signals + new-company discovery (news, HN, github,
                    funding, page changes, CSE searches)      ──▶ signals + candidates
   │
   ▼
scoring/rules ── the deterministic gate (your hunting profile:
                 roles, levels, locations, dealbreakers)
scoring/llm_bulk ── cheap cached scoring of rule-passed postings
   │
   ▼
inbox (the dashboard) ── you mark postings interested
   │
   ▼
packets/orchestrator ── builds the application packet: fill sheet from
   the master resume, tailored resume, claim-checked cover letter, PDF
   │
   ▼
webapp/runners/apply ── launch: automated (JobPilot fillers, headed
   browser, tiled windows) or assisted (browser opened at the form)
```

The **agent** (`agent/`) sits above the sources: a morning LLM tool-loop that
reads the brief (your hunting profile + today's signals + caps), does real web
searches (4 selectable engines), adds candidate companies to the watchlist,
records signals, and writes research notes + the morning report.

## The layout (one job per area)

```
jobscout/                 the pipeline package (cli.py is the only root module)
  core/                   kernel: paths · config · schema · db · resume · watchlist
  sources/postings/       ATS boards + dark-pool careers + job-site search → postings
  sources/discovery/      signal + company discovery (cse, github, hn, news, rss, monitoring)
  scoring/                gates: rules (deterministic) + llm_bulk (cheap tier)
  agent/                  judgment: brief · harness (tool loop) · tools
  packets/                applications: field_map · tailor · cover_letter · claim_check · render
  clients/                external service clients: llm · sidecar (JobPilot)
  ops/                    operational verbs: run · digest · doctor
  webapp/                 dashboard: routers/ (per page area) · stores/ (config
                          editors on one engine) · runners/ (agent hunts, applies)
config/ master_resume/    inputs (user-editable; shipped in the dmg)
var/                      ALL runtime outputs: data · digest · logs · morning_reports · research · applications
desktop/                  the Tauri shell + PyInstaller freeze (see desktop/README.md)
tests/                    conftest + one file per area
```

The package map in `jobscout/__init__.py` carries the same story inline.

## The config files (all inputs, all editable on the frontend)

| File | Holds | Frontend |
|---|---|---|
| `config/settings.yaml` | discovery mode (off/pipeline/agent/hybrid), agent caps (steps, $/run, schedule), search engine selection | Discovery + Ops pages |
| `config/profile.yaml` | the hunting profile (roles, levels, locations, stack) + scoring rubric + dealbreakers | Discovery page (Hunting profile card) |
| `config/models.yaml` | per-tier model slugs + prices + spend caps | Ops page (Models & pricing, with provider price refresh) |
| `config/watchlist.yaml` | the company substrate: A/B/C tiers, candidates, ATS tokens, notes | Companies page (editor + seed-by-URL) |
| `master_resume/resume.yaml` | the single source of truth for every application field | Profile page (form + full YAML upload/download) |
| `.env` | LLM key, CSE/Brave search keys (gitignored — the only secret store) | Ops page (credentials cards) |

## The three tiers (cost model)

| Tier | What | Cost |
|---|---|---|
| 0 | deterministic collectors (ATS JSON APIs, crawls, RSS, job-site search — MCF's public API + the Playwright sidecar for the bot-walled boards) | $0 |
| 1 | cheap LLM bulk scoring (cached by content hash) | pennies/day |
| 2 | full agent morning discovery (behind the mode switch) | dimes/day, capped |
| 3 | quality LLM packet tailoring (only postings you select) | ~$0.05–0.15 each |

## Build from source (dev)

Requirements: Python ≥ 3.11, Node ≥ 18 (desktop + JobPilot), Rust
(`rustup`, for the Tauri shell), optionally `typst` for PDFs
(`brew install typst`). For the bot-walled job sites (LinkedIn, Indeed,
YC, Wellfound) the JobPilot sidecar also needs the sibling repo built
(see below) — without it the sweep degrades to the plain-API sites
(MyCareersFuture).

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[all]"          # app + web + dev deps
.venv/bin/pip install pyinstaller          # for release builds
.venv/bin/jobscout db init                 # database (var/data/)
.venv/bin/jobscout doctor                  # health check
.venv/bin/jobscout serve --port 8799       # dashboard at http://127.0.0.1:8799
```

Tests: `.venv/bin/pytest tests/` (228 tests; fakes only — no network, no
browser launches, no LLM calls).

## Build the desktop app (release)

```bash
cd desktop
npm install
npm run dev              # dev: repo backend + webview window
npm run build:release    # freeze + bundle → jobscout.app + jobscout_*.dmg (~2 min)
```

`build:release` snapshots your live `config/` + `master_resume/` into the
bundle (the dmg ships your current setup), freezes the Python backend with
PyInstaller, and builds the Tauri shell. Full story + copying to another
Mac: [desktop/README.md](desktop/README.md).

## Deploy (target Mac only — never the dev Mac)

```bash
./bootstrap.sh --dry-run           # see what it would do
./bootstrap.sh --with-agent --with-dashboard
```

Or just AirDrop the built dmg — self-contained, seeds its own runtime on
first launch at `~/Library/Application Support/com.jobscout.desktop/`.

## CLI reference

```
jobscout run [--daily] [--force]   the sweep: postings + signals + scoring
jobscout agent [--morning]         the agent tool-loop (needs LLM key)
jobscout add-company NAME --domain d [--tier B]   seed the watchlist (probes ATS)
jobscout probe --domain d          probe a domain for ATS boards
jobscout digest [--today]          show the latest morning digest
jobscout score / stats / prepare / mark / scan-form / fill / retro
jobscout init-home                 bootstrap a relocated runtime (packaged app)
jobscout serve [--port N]          the dashboard
jobscout db init|status            database lifecycle
jobscout config check|show         config validation
jobscout resume validate|fields    master resume checks
jobscout doctor                    whole-stack health check
```

## Environment variables

| Variable | Effect |
|---|---|
| `JOBSCOUT_HOME` | relocates the ENTIRE runtime (the packaged app sets it) |
| `JOBSCOUT_BIN` / `JOBSCOUT_NODE_BIN` / `JOBSCOUT_SIDECAR_BIN` | override binary resolution (bundled sidecar) |
| `JOBSCOUT_AGENT_FOCUS` | `profile` = agent spends its whole budget on profile-driven employer discovery |

Sibling repo `../JobPilot` (Node/Playwright sidecar) is the "hands" — spawned as
a subprocess, no rewrite. It powers two optional paths: the automated apply
fillers (without it: assisted mode, browser opened at the form) and the
bot-walled job-site scrapers — LinkedIn/Indeed/YC/Wellfound via Playwright
(Indeed launches your real Chrome; YC is a plain fetch). Without it the daily
sweep still runs: MyCareersFuture's public API + the ATS boards + the
discovery sources. Build it once: `cd ../JobPilot/scraper && npm install &&
npm run build-internal` (plus `npx playwright install chromium`).
