# clients/ — external service clients

One module per external service the app talks to. Both are plain
subprocess-or-HTTP clients with no knowledge of the pipeline.

## llm.py — the LLM client

`LlmClient` talks to any OpenAI-compatible endpoint (OpenRouter, DeepSeek
direct, ...). Key facts:

- **Tiers** (`config/models.yaml`): `bulk` (scoring, pennies), `agent`
  (the tool loop), `quality` (tailoring/cover letters) — each with its own
  model slug, prices (per-mtok, for the spend meter), and daily $ cap.
- `chat(tier, messages)` — the main call: cap gate, JSON mode with
  fallback, 3 retries (5xx/429), tool-calling support, response metering
  into `llm_calls` (tier, model, tokens, cost, cache key).
- `complete(model, messages)` — the raw-completion primitive (arbitrary
  slug, no tier lookup/cap/cache): shares auth/base/retries/metering with
  chat(); used by the agent's LLM-search engine.
- Key/base resolve from env + `.env` (file wins); `.env` is re-read on
  every construction, so a saved key is live without restart.

## sidecar.py — the JobPilot client

`SidecarClient` spawns the Node sidecar (`../JobPilot/scraper/dist/index.js`)
and speaks its line protocol (JSON per line, keyed by request id):

- Actions: `ping`, `getAllScrapers/Fillers`, `scrapeJobs`, `scanForm`,
  `applyJobsByPayload`, `cancelApplyJobs`, `killServer`.
- `apply_jobs_by_payload(jobs, profile, settings)` — enqueue a headed apply
  run; the response id keys all later events.
- `scrape_sites(scraper_ids, keywords, location, top_n, timeout)` — scrape
  job boards: one `scrapeJobs` request, then drains events until the
  sidecar's `scrape:all-done` (EndMsg). Per-scraper `scrape:done` results
  are **merged** (each board emits its own result — a replace would drop
  every board but the last; pinned by test). Returns JobDetails dicts;
  `sources/postings/sites.py` converts them to `RawPosting`s.
- `drain_events(req_id)` — pops accumulated `apply:update` events
  (`record.job.id` = the packet id) for the run tracker; also how
  `scrape_sites` watches the scrape stream.
- Binary resolution: `JOBSCOUT_BIN`/`JOBSCOUT_SIDECAR_BIN`/`JOBSCOUT_NODE_BIN`
  env overrides; absent → degraded gracefully by callers (assisted mode).

**Tests:** `tests/test_llm_bulk.py`, `tests/test_apply.py` (fakes — no
network, no sidecar process).
