"""jobscout — cost-effective daily AI job-hunt pipeline.

Package map (one job per area; README.md carries the full tree):

  core/      the kernel: paths (runtime roots, JOBSCOUT_HOME), config
             loading, schema (config models), db (schema + queries),
             resume loading, watchlist (the company substrate + its one
             mutation layer)
  sources/   where data enters:
               postings/  — ATS boards + dark-pool careers pages → postings
               discovery/ — signal + company discovery (cse, github, hn,
                            news, rss, monitoring)
  scoring/   the gates: rules (deterministic) + llm_bulk (cheap tier)
  agent/     the judgment layer: brief, harness (tool loop), tools
  packets/   applications: field_map, tailor, cover_letter, claim_check,
             render (typst), orchestrator
  clients/   external service clients: llm (LLMClient), sidecar (JobPilot)
  ops/       operational verbs: run (daily orchestrator), digest
             (morning digest writer), doctor (health checks)
  webapp/    the dashboard:
               routers/  — one module per page area
               stores/   — the config editors, all on one engine
               runners/  — background orchestration (agent hunts, applies)
  cli.py     the verb surface (the only module at the package root)

Repo layout: source (jobscout/, desktop/, tests/) · inputs (config/,
master_resume/) · outputs (var/) · docs.
"""

__version__ = "0.1.0"
