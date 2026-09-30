# ops/ — the operational verbs

The daily-cycle commands behind the CLI verbs and the webapp buttons.

## run.py — the daily orchestrator (`jobscout run --daily`)

One pass, in order:

1. **Postings**: for every watchlist entry with ATS tokens, poll the board
   fetchers (`sources/postings`), dedupe by url/content hash, upsert;
   dark-pool entries get the careers crawl (`postings/careers.py`) which
   also discovers ATS boards and pins tokens back into the watchlist.
2. **Scoring**: rule gate on new postings (`scoring/rules`), then cheap
   LLM bulk scoring (`scoring/llm_bulk`) if a key is set.
3. **Discovery sweep** (`sources/discovery.run_sweep`): signals + candidate
   companies (news, HN, github, funding, CSE, monitoring).
4. **Job-site search** (`sources/postings.sites.sweep_sites`, behind
   `settings.discovery.pipeline.job_sites`): MyCareersFuture's public API
   (per role × location) + the sidecar boards — keyword batch (LinkedIn,
   Indeed, Wellfound) per role, plus one keyword-less YC browse batch.
   ~220 postings/sweep when the sidecar is built; HTTP-only otherwise.
5. **Monitoring**: page-hash + sitemap change detection → signals.
6. Records the run in the `runs` table; idempotency: skips if the last
   good run is < `skip_if_run_within_hours` old (unless `--force`).

This is what "full hunt" on the Discovery page chains, followed by the
morning agent. `JOBSCOUT_HOME` relocates the entire runtime.

## digest.py — the morning digest writer

Writes `var/digest/YYYY-MM-DD.md`: the day's new postings (rule-passed,
scored), fresh signals, watchlist growth, monitoring changes, and the
spend footer. `jobscout digest` shows the latest.

## doctor.py — the health checks

`jobscout doctor`: repo layout, config files parse, resume schema, DB
schema version, `.env` presence, sidecar availability (informational —
warn, not fail). Exit 1 on any failure; the run gate for deploys.

**Tests:** `tests/test_monitoring.py`, `tests/test_discovery.py`,
`tests/test_webapp.py` (run orchestration via the routers).
