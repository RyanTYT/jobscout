# sources/ — where data enters

Two packages, one split: **postings** produce job listings; **discovery**
produces signals and new companies. Sources never score, never render —
they fetch and normalize.

## postings/ — job listings → the `postings` table

| Module | Job |
|---|---|
| `greenhouse.py` / `lever.py` / `ashby.py` / `smartrecruiters.py` | Board fetchers — one per ATS. Each exposes `fetch(token, client) -> list[RawPosting]` and is registered in `FETCHERS` (keyed by provider). |
| `base.py` | Shared plumbing: `RawPosting` (title/url/company/location/description/seniority...), `make_client` (polite httpx client), `soft_get` (never-raises GET), `strip_html`. |
| `probe.py` | `probe_company(name/domain)` — guesses board tokens (greenhouse/lever/ashby/smartrecruiters), verifies live, returns provider→token. Used by `add-company`, the seed-by-URL route, and the discovery `add_candidate`. |
| `careers.py` | The dark-pool source: `crawl_company(domain, name)` finds the careers page (sitemap + homepage heuristics), extracts postings (JSON-LD), and *also* probes ATS boards. Dark-pool = no public board; monitored via signals instead. |

## discovery/ — signals + candidates → `signals` table + watchlist candidates

`run_sweep(client, conn, wl, profile, settings, env)` runs every source
(free HTTP), appends watchlist candidates via `add_candidate` (which
live-probes ATS boards), and returns stats.

| Module | Signal |
|---|---|
| `news.py` | Google News RSS per watchlist company → `news` |
| `rss.py` | Configurable feeds (techcrunch funding/fintech, coindesk) → `funding` |
| `hn.py` | HN Algolia: who-is-hiring comments (hiring-context filter) + company mentions → `hn_mention` |
| `github.py` | Recent org repo pushes (org guessed from domain, cached) → `github_activity` |
| `cse.py` | Deterministic daily Google searches (query bank rotates profile roles/domains, `cse_queries_per_day` cap) → candidates + `ats_found` signals. Needs CSE keys. |
| `monitoring.py` | Dark-pool watching: careers-page content hash + sitemap URL set diffs → `careers_page_changed` / `sitemap_new_url` |
| `blocklist.py` | Domain/company blocklist for the sweep. |

**Signals are idempotent** — keyed by (kind, key) hash; re-runs never
duplicate. **Tests:** `tests/test_monitoring.py`,
`tests/test_discovery.py`, `tests/test_careers_page.py`.
