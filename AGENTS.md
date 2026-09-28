# AGENTS.md — conventions for coding agents working in this repo

## First, always

1. Read `PLAN.md` **Appendix A** (resume-from-anywhere protocol) and the status block at its top.
2. `PLAN.md §10` checkboxes = the work queue. First unchecked item in the current phase is your task.
3. After any structural change: run `jobscout doctor`. After `master_resume/` changes: `jobscout resume validate`.

## Hard rules

- **Never commit** `.env`, `data/`, `logs/` — they contain the owner's private data and keys.
- **Never run `bootstrap.sh` on this (dev) Mac.** It installs launchd agents; it belongs on the deployment Mac only.
- **No auto-submission of job applications**, ever. Default flow ends at a packet; "fill for me" is headful with a human pressing submit.
- **No fabricated resume facts.** Generated text must trace to `master_resume/` (claim-check, PLAN §5.7). If a required fact is missing, surface it as `needs_input` — do not invent it.
- **LLM spend is capped.** Tier caps live in `config/models.yaml`. Never bypass the spend meter.
- Mandates in `PLAN.md §0` outrank convenience and outrank stale code.

## Code conventions

- Python ≥ 3.11, stdlib-first. Only deps in `pyproject.toml`. No npm/node anywhere in jobscout (that's JobPilot's world).
- **CLI-first design**: every capability is a `jobscout <verb>`. Dashboard and agent both call the same core functions.
- Schemas: `jobscout/core/models.py` (pydantic) is the executable form of `PLAN.md §5`. If you change one, change both in the same commit.
- SQLite via `jobscout/core/db.py`. WAL mode. Never talk to the DB with ad-hoc SQL outside `core/` — add a function there.
- Config lives in `config/*.yaml`, validated by pydantic models in `core/models.py`. No config in code.
- Style: ruff (`line-length=100`), type hints, small modules. No comments narrating the obvious.
- IDs in `master_resume/resume.yaml` are **stable forever** (EXP1-B2 etc.) — tailoring and claim-checks reference them. Never renumber.

## Where things go

| Adding a… | File |
|---|---|
| ATS/board source | `jobscout/sources/ats/<name>.py` |
| Discovery source (search/RSS/HN) | `jobscout/sources/discovery/<name>.py` |
| LLM prompt/step | `jobscout/scoring/` or `jobscout/packets/` or `jobscout/agent/` by phase |
| Dashboard page | `jobscout/webapp/` (FastAPI + Jinja2 + HTMX, no build step) |
| Schema change | `core/models.py` + `PLAN.md §5` + a migration in `core/db.py` |

## When you finish

Tick the `PLAN.md §10` checkbox, update the status block, add a Decision Log
entry if you changed a decision, run `jobscout doctor`. Leave the repo green.
