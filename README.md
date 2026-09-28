# jobscout

Cost-effective daily AI job-hunt pipeline. An agent-driven discovery layer (switchable),
a free deterministic collection layer, cheap cached LLM scoring, and grounded
application packets built entirely from a single master resume.

**Read [PLAN.md](PLAN.md) first** — it is the single source of truth for design
and progress. [AGENTS.md](AGENTS.md) covers conventions for coding agents.

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
