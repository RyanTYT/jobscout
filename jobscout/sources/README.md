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
| `sites.py` | Keyword job-site search — TWO seams: plain-API sites (`SITES`: MyCareersFuture's public v2 JSON API, no auth, verified live) and bot-walled boards through the JobPilot Playwright sidecar (`SIDECAR_KEYWORD_SITES`: LinkedIn, Indeed, Wellfound — scraped per role WITH keywords; `SIDECAR_BROWSE_SITES`: YC — scraped once keyword-less, its guest role pages ARE the search surface, no keyword search without login). `sweep_sites(client, profile)` runs both, dedups by URL, always stops the shared sidecar in `finally`. Degrades to HTTP-only when the sidecar isn't built. Seniority mapping: the sidecar's "mid" → None (unlabeled — passes the junior/mid gate to LLM scoring); senior/staff/lead/... kept as tokens. |

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
`tests/test_discovery.py`, `tests/test_careers_page.py`,
`tests/test_routes_applications.py` (the site sweep + sidecar conversion,
with fakes — no network, no browser).

### The job-site sweep (sites.py) — what to expect

| Site | How | Honest limits |
|---|---|---|
| MyCareersFuture (SG) | public v2 JSON API, no auth, no browser | official government board; clean data |
| LinkedIn | sidecar Playwright, guest search | ~guest results; flaky per-request (bot wall) |
| Indeed | sidecar, launches your **real Chrome** (channel) | Cloudflare wall flaps; retries + honest partials; `l=` geo filter is loose on www |
| Y Combinator (WAS) | sidecar, plain **fetch** (Inertia data-page JSON) | guest cap: 30 jobs/role page, no keyword search without login |
| Wellfound | sidecar Playwright | US-focused; usually 0 for SG queries |

The orchestrator-side keyword filter is phrase-substring (title/tags),
which is why YC runs keyword-less in its own batch — see `sweep_sites`.
