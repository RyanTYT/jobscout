# jobscout

Cost-effective daily AI job-hunt pipeline. An agent-driven discovery layer (switchable),
a free deterministic collection layer, cheap cached LLM scoring, and grounded
application packets built entirely from a single master resume.

**Read [PLAN.md](PLAN.md) first** — it is the single source of truth for design
and progress. [AGENTS.md](AGENTS.md) covers conventions for coding agents.

## The layout (one job per area)

```
jobscout/                 the pipeline package (cli.py is the only root module)
  core/                   kernel: paths · config · schema · db · resume · watchlist
  sources/postings/       ATS boards + dark-pool careers pages → postings
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
desktop/                  the Tauri shell + PyInstaller freeze
tests/                    conftest + one file per area
```

The package map in `jobscout/__init__.py` carries the same story inline.

## Quickstart (dev)

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[all]"
.venv/bin/jobscout doctor          # health check
.venv/bin/jobscout resume validate # after filling master_resume/resume.yaml
```

## Deploy (target Mac only — never the dev Mac)

```bash
./bootstrap.sh --dry-run           # see what it would do
./bootstrap.sh --with-agent --with-dashboard
```

## The three tiers (cost model)

| Tier | What | Cost |
|---|---|---|
| 0 | deterministic collectors (ATS JSON APIs, crawls, RSS) | $0 |
| 1 | cheap LLM bulk scoring (cached by content hash) | pennies/day |
| 2 | full agent morning discovery (behind the mode switch) | dimes/day, capped |
| 3 | quality LLM packet tailoring (only postings you select) | ~$0.05–0.15 each |

Sibling repo `../JobPilot` (Node/Playwright sidecar) is the "hands" — spawned as
a subprocess in Phase 7, no rewrite.
