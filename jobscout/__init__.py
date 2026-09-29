"""jobscout — cost-effective daily AI job-hunt pipeline.

Package map (one job per area; see README.md for the full tree):

  core/     the kernel: paths (runtime roots, JOBSCOUT_HOME), config
            loading, schema (config models), db (schema + queries),
            resume loading
  sources/  where data enters:
              postings/  — ATS boards + dark-pool careers pages → postings
              discovery/ — signal + company discovery (cse, github, hn,
                           news, rss, monitoring)
  scoring/  the gates: rules (deterministic) + llm_bulk (cheap tier)
  agent/    the judgment layer: brief, harness (tool loop), tools
  packets/  applications: field_map, tailor, cover_letter, claim_check,
            render (typst), orchestrator
  webapp/   the dashboard: routers/ (one per page area), the config
            editors (stores on one engine), agent_runner, apply
  cli.py    the verb surface            run.py      the daily orchestrator
  digest.py morning digest writer       doctor.py   health checks
  llm.py    the LLM client              sidecar.py  the JobPilot client
  watchlist.py the company substrate (the one mutation layer)

Repo layout: source (jobscout/, desktop/, tests/) · inputs (config/,
master_resume/) · outputs (var/) · docs.
"""

__version__ = "0.1.0"
