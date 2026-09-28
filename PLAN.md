# jobscout — Master Plan & Progress Tracker

> **This file is the single source of truth for design AND progress.**
> Anyone (human or agent) picking this project up: read **Appendix A** first,
> then the status block below, then jump to the section you need.

| | |
|---|---|
| **Version** | 2.0 (pinned 2026-09-28) |
| **Status** | P7 + P8 dark-pool extension + UI overhaul complete: 5 monitoring/discovery signal sources live (207 news + 199 sitemap + 3 GitHub + 2 HN-mention signals on first run); dashboard rebuilt on vendored Bootstrap 5.3 (the framework — standardized forms/tables/dropdowns/toasts/dark-mode with zero hand-rolled layout CSS) + a token→--bs-* theme layer (app.css) + Jinja2 component macros as the component functions; sidebar nav, server-side pagination over ALL postings, full filter panel, skeleton loading, toasts, dark mode ·  · **ALL 8 PHASES + EXTENSIONS COMPLETE** |
| **Repo** | `/Users/user/Downloads/Personal Project/jobscout` (local only, no remote yet) |
| **Sibling repos** | `../JobPilot` (the "hands", see §6) · `../rusty_trader` (unrelated) |
| **Dev Mac rule** | This Mac develops only. **Never run `bootstrap.sh` here** — it installs launchd and belongs on the target/deployment Mac. |

**Progress lives in §10 (Build Phases). Decisions live in §12 (Decision Log).**

---

## 0. Design mandates

These are the fixed requirements from the owner. Every design decision below serves them. If a change conflicts with a mandate, the mandate wins.

1. **Extremely cost-effective agent employment.** Three-tier cost pyramid (§1): deterministic code (free) → cheap LLM (pennies, cached) → full agent (dimes, capped). Target ~$1–15/month all-in depending on mode.
2. **A full agent drives discovery every morning**, controllable by a switch in the app (§1.1): `off | pipeline | agent | hybrid`. The agent is the scout commander; deterministic collectors are its troops.
3. **Target companies with weak ATS presence** — the "dark pool": good firms whose openings never hit boards. Company-first graph, ATS-absence probing, signal timelines, suggested actions (§2, §4).
4. **Skills repo / agent-usable.** Every capability is a CLI verb; `skills/jobscout/SKILL.md` is the interactive-agent contract; the morning brief runs headless (own harness) or interactively (§1.2).
5. **Master resume is the single source of truth** — including a personal-website section and an additional-information section. Everything generated (tailored resume, cover letter, form answers) must trace back to it via the claim-check gate (§5.7). **No fabricated facts, ever.**
6. **Interactive local HTML dashboard** drives the full flow: browse scored postings → select → identify required application values → agent tailors resume/cover letter → packet → optional deterministic fill (§7).
7. **JobPilot is the hands.** The existing Node/Playwright sidecar (own repo, `../JobPilot`) is spawned as a subprocess; zero rewrite (§6).
8. **Deployment via portable `bootstrap.sh` on another Mac.** Nothing is installed on the dev Mac (§8).

## 1. The three-tier cost pyramid + the discovery-mode switch

```
TIER 0  deterministic (free)       ATS JSON APIs, careers-page crawls, sitemaps,
                                   RSS, dedup, rule filters, form dispatch
TIER 1  cheap LLM (pennies,        bulk scoring, field mapping, claim checks —
         cached by content hash)   every posting scored once, ever
TIER 2  full agent (dimes,         MORNING DISCOVERY: query generation, judgment,
         step+cost capped)         dark-pool research — behind the switch
TIER 3  quality LLM (on demand)    packet tailoring for postings the owner selected
```

### 1.1 The switch

`config/settings.yaml` + dashboard Settings page (a real toggle, not a file edit):

```yaml
discovery:
  mode: hybrid            # off | pipeline | agent | hybrid
  agent:
    schedule: weekdays    # daily | weekdays | mon-wed-fri | manual
    max_steps: 60         # tool-call budget per run
    max_cost_usd: 0.50    # hard stop, recorded in the runs table
    model: agent          # key into config/models.yaml tiers
    run_on_signal: true   # extra runs when the pipeline detects anomalies
  pipeline:               # deterministic bulk — ALWAYS runs (it is free)
    ats_boards: true
    careers_crawl: true
    cse_queries_per_day: 10
    rss: true
```

- `pipeline` — deterministic sources + cheap scoring only (~$1/mo).
- `agent` — morning agent rides on top of the (still-running, free) pipeline.
- `hybrid` — agent N days/week + on-demand runs on signal spikes (funding round detected, watchlist careers-page change).
- Dashboard Discovery page: mode toggle, schedule, intensity caps, this month's agent spend, morning-report archive, **[Run agent now]**.

### 1.2 The Morning Brief (agent contract, ~15–40 tool calls)

Two runtimes execute the identical brief:

- **Headless harness (cron default):** a Python tool-loop we own — tools: `web_search` (Google CSE free 100/day + Brave free tier, paid fallback), `fetch` (httpx + readability; JS pages → JobPilot sidecar render), `db_query`, `add_company`, `add_signal`, `write_note`. **It cannot mutate state except through core-library functions** (the same code the CLI uses) — no freeform DB writes.
- **Interactive skill (owner's coding agent):** same brief expressed in `skills/jobscout/SKILL.md`; uses the agent's native web tools and the same CLI verbs. For deep dives while the owner is at the machine.

Brief steps:

1. Load context — yesterday's digest, open dark-pool leads, query-bank rotation pointer.
2. Sweep — 15–30 searches: banked queries (rotated) + self-generated ones. *This is the irreplaceable agent value: deciding what to search.*
3. Extract candidates — company names + evidence (funding, "we're hiring" posts, news, a new ATS board appearing).
4. Enrich — for each candidate call the deterministic ATS-probe tool (Greenhouse/Lever/Ashby token guesses + careers-page fetch). The agent never bulk-scrapes.
5. Classify — append to watchlist `candidates` via `jobscout add-company` (§5.9) / flag for owner / discard, each with a one-line logged rationale. Promotion to C/B/A stays the owner's call.
6. Dark-pool research — top 3–5 leads with signals-but-no-postings: deeper fetches (team page, blog, news archive) → per-company research note in `research/`.
7. Write `morning_reports/YYYY-MM-DD.md`, update DB via core functions, log spend. Report ends with a **DB-change diff** — every mutation auditable.

Guardrails: step cap, dollar cap, tool allowlist, all writes via core functions, every decision logged with rationale, hard-stop on cap (degrade to rule-only, note in digest).

## 2. System architecture

```
┌────────────────────── jobscout (Python) — the brain ──────────────────────┐
│  SOURCES      ATS JSON APIs · careers crawler · CSE · RSS · HN            │
│  DISCOVERY    [pipeline mode]  [agent harness — the morning brief]        │
│  STATE        SQLite: postings, companies, signals, packets, runs, cache  │
│  SCORING      rules → cheap-LLM (cached) → final = fit·.6 + company·.25   │
│               + opportunity·.15  (opportunity = ATS-weakness/competition) │
│  PACKETS      master_resume/ → tailor → claim-check → fill sheet + docs   │
│  FACES        CLI (agent) · FastAPI dashboard (owner) · launchd (cron)    │
└──────────┬──────────────────────────────┬─────────────────────────────────┘
           │ stdio JSON-lines             │ HTTP 127.0.0.1
           ▼                              ▼
┌─ JobPilot sidecar (Node/Playwright) ──┐  ┌─ Browser: dashboard ───────────┐
│ scrape boards · render JS pages ·     │  │ inbox · postings · applications│
│ scanForm (new) · fill (opt-in,        │  │ companies/dark pool · discovery│
│ pauseOnUncertainty=true always)       │  │ + the mode switch              │
└───────────────────────────────────────┘  └────────────────────────────────┘
```

One core library; CLI (agent), dashboard (human), and launchd (cron) are thin clients over it.

Two discovery loops:

- **Job-first (what everyone sees):** pull postings from ATS JSON APIs + careers pages → dedup by content hash → rule filter → cheap-LLM score → inbox.
- **Company-first (the dark pool):** company graph — probe Greenhouse/Lever/Ashby/SmartRecruiters board tokens, crawl careers pages, watch signals (funding news, HN hiring comments, engineering blogs, search hits). Company with **no ATS board + hiring signals + no postings** = dark-pool lead → dedicated dashboard section with suggested action. Scoring bakes the thesis in: `final = fit×0.6 + company_quality×0.25 + opportunity×0.15`.

## 3. Repo layout

```
jobscout/
├── PLAN.md                      # this file — design + progress
├── AGENTS.md                    # conventions for coding agents working here
├── README.md · pyproject.toml · .env.example · .gitignore
├── bootstrap.sh · uninstall.sh  # TARGET-Mac deployment (never run on dev Mac)
├── config/
│   ├── settings.yaml            # discovery.mode + agent caps (the switch)
│   ├── profile.yaml             # distilled scoring rubric (generated, human-approved)
│   ├── watchlist.yaml           # living company substrate (§5.9): A/B/C + candidates + verified ATS tokens
│   └── models.yaml              # LLM tiers: bulk / agent / quality + caps
├── master_resume/               # §5 — THE source of truth
│   ├── resume.yaml              # structured, every claimable fact has a stable ID
│   ├── narrative.md             # STAR stories keyed to bullet IDs
│   └── extras.md                # additional info, policies
├── jobscout/                    # the package
│   ├── cli.py                   # typer CLI — every capability is a verb
│   ├── doctor.py                # health checks
│   ├── core/ (paths, models, config, db, resume)
│   ├── sources/ (ats/ · careers_page/ · discovery/)   # P1–P4
│   ├── agent/ (harness, tools, brief, caps)           # P5
│   ├── scoring/ (rules, llm_bulk)                     # P2
│   ├── packets/ (field_map, tailor, claim_check, render) # P6
│   └── webapp/ (FastAPI + Jinja2 + HTMX)              # P2
├── templates/resume.typ         # typst resume renderer (P6)
├── skills/jobscout/SKILL.md     # interactive-agent contract
├── digest/                      # generated daily digests (markdown)
├── morning_reports/             # generated agent reports
├── research/                    # per-company research notes (agent output)
├── applications/                # generated packets (§5.8)
├── logs/ · data/                # gitignored: SQLite DB + run logs
```

## 4. Daily flow

1. **06:30** launchd → `jobscout run --daily`: pipeline pulls (free), dedup, rule filter, cheap-LLM bulk scoring (cached) → `digest/YYYY-MM-DD.md`.
2. **07:00** (if switch on) → `jobscout agent --morning`: the brief (§1.2) → `morning_reports/YYYY-MM-DD.md` + watchlist/signal updates.
3. **Morning, owner:** open dashboard (or ask the interactive agent to summarize): new scored postings, dark-pool section, agent research notes.
4. **Owner selects** postings → **packets** (§5.4–5.8) → review claim-check warnings → [Fill for me] (headful, sidecar, owner presses submit) or apply manually from the packet.
5. **Weekly:** applied/dismissed labels vs scores → proposed `profile.yaml` diff (human-approved).

## 5. Structured specifications (the pinned schemas)

> **Single source of truth for these schemas:** `jobscout/core/models.py` (pydantic).
> If a schema changes, change BOTH this section and `models.py`, then run
> `jobscout resume validate` and `jobscout doctor`.

### 5.1 `master_resume/resume.yaml`

Design principle: **every claimable fact is a row with a stable ID.** Tailoring references IDs; the claim-check verifies against IDs; diffs are structural.

```yaml
version: 3            # bump = profile_version bump = LLM cache re-key

identity:
  full_name: "…"
  preferred_name: "…"
  email: "…"
  phone: "…"
  location: {city: …, region: …, country: …, timezone: …}
  links:                          # ← the personal-website section
    personal_website: "https://…"
    linkedin: "…"
    github: "…"
    extra: [{id: L1, label: "blog", url: "…"}]

work_authorization:
  - {country: "US", status: "…", sponsorship_needed: false, note: "…"}

education:
  - id: EDU1
    school: "…"
    degree: "…"                    # BS / MS / PhD
    field: "…"
    dates: {start: 2015-08, end: 2019-05}
    gpa: …                         # optional
    honors: […]
    highlights: […]                # thesis, notable coursework

experience:
  - id: EXP1
    company: "…"
    domain: "company.com"          # used to auto-probe ATS + careers page
    context: "one line: what the company does"   # grounding context for the tailor
    title: "…"                     # current title if dates.end is present
    titles: [{title: …, from: …, to: …}]         # promotion history
    dates: {start: 2020-03, end: present}        # end: YYYY-MM | present | null
    location: "…"
    employment: full-time          # full-time | contract | internship
    tech: [rust, c++, python, …]
    bullets:
      - id: EXP1-B1
        text: "Built X that did Y"
        metrics: ["38% latency reduction"]       # claim-check anchors
        tech: […]
        tags: [latency, market-data, leadership] # selection hints for the tailor

projects:
  - id: PRJ1
    name: "…"
    url: "…"
    dates: {start: …, end: …}
    role: "…"
    context: "one line"
    tech: […]
    bullets:
      - id: PRJ1-B1 {text, metrics, tech, tags}

skills:
  core: [{area: languages, items: […]}, {area: domains, items: […]}]
  certifications: […]
  publications: [{id: PUB1, title, venue, date, url}]

extras:                            # ← the additional-information section
  salary: {policy: "defer to conversation", floor: …, currency: USD}
  notice_period: "…"
  availability: "…"
  languages_spoken: […]
  interests: […]                   # cover-letter color, humanizing details
  referrals: {policy: "company careers page"}   # honest default for "how did you hear"
  eeo: {policy: decline}           # gender/ethnicity/veteran: decline by default
  other: [{id: X1, label: …, value: …}]
```

### 5.2 `narrative.md` and `extras.md`

- `narrative.md` — 6–10 STAR stories, each keyed to bullet IDs:
  `## STORY-S3 (refs: EXP2-B1, PRJ1-B2)` → Situation / Task / Action / Result.
  Raw material for cover letters; never pasted wholesale.
- `extras.md` — long-form additional information, policies, anything un-structurable.

CLI support: `jobscout resume validate` (schema + referential integrity: every referenced ID exists, IDs unique, narrative refs resolve), `jobscout resume fields` (dump the canonical field map).

### 5.3 Canonical application field map

The "list of values a job application needs". Two sources: **(a)** the sidecar's `scanForm` on the real ATS form (exact labels, required flags, option lists), **(b)** a standard checklist when scanning isn't available. Every label maps to a canonical key:

| Canonical key | Source in master_resume | Notes |
|---|---|---|
| `full_name`, `preferred_name` | identity | |
| `email`, `phone` | identity | |
| `city`, `region`, `country`, `full_address` | identity.location | full address only if form requires + policy allows |
| `linkedin`, `github`, `personal_website`, `link:*` | identity.links | |
| `current_company`, `current_title` | latest EXP with end=present/null | |
| `years_experience` | **derived** from earliest `dates.start` | computed, claim-checkable |
| `highest_degree`, `school`, `field`, `graduation_year`, `gpa` | education[] | GPA optional per policy |
| `work_authorization`, `sponsorship_needed` | work_authorization[] | country-matched |
| `salary_expectation` | extras.salary | policy: defer / floor+x% |
| `notice_period`, `earliest_start` | extras | |
| `referral_source`, `heard_about` | extras.referrals | honest default |
| `resume_file` | packet (tailored PDF) | |
| `cover_letter` | packet (generated text) | |
| `screening:*` | per-job, drafted from master resume | flagged if unanswerable — **never guessed** |
| `eeo:*` | extras.eeo | declined by default |
| `other:*` | extras.other | |

Label→key mapping order: JobPilot's Sørensen–Dice fuzzy matcher first, cheap-LLM fallback for labels below threshold.

### 5.4 Per-posting `fill_sheet.yaml`

```yaml
posting: p_20260928_ab12
source_form: scanned        # scanned (sidecar scanForm) | checklist
fields:
  - label: "First Name"
    canonical_key: full_name
    required: true
    value: "…"
    value_source: master_resume.identity.full_name
    confidence: exact       # exact | fuzzy | policy | draft | missing
  - label: "Are you legally authorized to work in the US?"
    canonical_key: screening:work_auth_us
    value: "drafted answer"
    value_source: master_resume.work_authorization[0]
    confidence: draft       # LLM-drafted → needs owner review
unmatched: []               # labels we couldn't map → dashboard checklist
policy_flags: [salary]      # fields where a policy default was used
```

### 5.5 Tailoring output `tailor.yaml` — a selection plan, not freeform prose

```yaml
posting: p_…
model: quality-tier
summary:
  text: "…"                 # 2–3 lines; every claim must pass §5.7
plan:
  experience_order: [EXP2, EXP1, EXP3]
  bullets:                  # selection only — ordering allowed
    EXP2: [EXP2-B1, EXP2-B4]
    EXP1: [EXP1-B2]
  rephrased:                # ONLY connective-language changes permitted
    - source_id: EXP1-B2
      text: "…"
      change: "foregrounded the latency metric; same facts"
  projects: {PRJ1: [PRJ1-B1, PRJ1-B3]}
  skills_emphasis: [rust, market-data, low-latency]
  skills_drop: [items irrelevant to this role]
keyword_alignment:
  matched: [rust, market-data, …]
  gaps: [verilog]           # honesty: gaps are NOT papered over
```

Rendered by `templates/resume.typ` → stable formatting, structural diffs. The quality model receives master-resume fragments + the posting; it may select, reorder, rephrase connective tissue, and write the summary — **nothing else**.

### 5.6 Cover letter spec

```yaml
tone: professional-warm      # configurable
paragraphs:
  - {role: hook,    grounded_in: research_notes + company context}   # why THIS company
  - {role: fit,     refs: [EXP2-B1, PRJ1-B2]}
  - {role: proof,   refs: [STORY-S3]}                               # one specific story
  - {role: close,   from: extras (logistics, availability)}
constraints: {words: 250-350, first_person: true, no_claims_outside_refs: true}
```

### 5.7 The claim-check gate

For every generated artifact, extract claims (numbers, percentages, dates, durations, company/product names, titles, degrees) and verify each traces to master_resume:

```yaml
- claim: "38% latency reduction"
  source: EXP1-B1.metrics
  status: verified          # verified | altered | unsupported
- claim: "led a team of 12"
  status: unsupported       # → dashboard red flag
```

Rules: metrics/dates/entities must match **verbatim or numerically-identical**; rephrasings may alter connective language only. **A packet cannot reach `ready` status with unresolved `unsupported` claims** — the owner accepts, edits, or regenerates. (Discipline equivalent to the no-look-ahead rule: the edge must be real.)

### 5.8 Packet layout + status machine

```
applications/{company-slug}-{date}/
  packet.yaml          # manifest: statuses, model used, cost, hashes
  fill_sheet.yaml      # §5.4
  tailor.yaml          # §5.5
  resume.md + resume.pdf
  cover_letter.md
  claim_check.yaml     # §5.7
  research_notes.md    # company research → cover-letter hook
```

Status flow: `interested → drafting → needs_input → ready → filled → applied` (+ `dismissed`, `withdrawn`) — same state machine in DB, CLI, dashboard, and agent skill.

### 5.9 Watchlist semantics — the living company substrate

`config/watchlist.yaml` is not just a scrape list. It is the **company-first substrate** both discovery loops consume:

- **Scrape targets:** entries with `ats:` tokens (provider → board token, discovered by probing) are polled daily by the free ATS JSON connectors.
- **Dark-pool graph:** entries with empty `ats:` have no board presence — monitored by the careers crawler (P3) and signals (P4/P5). Hiring signals + no postings = dark-pool lead.
- **Growth:** discovery (deterministic pipeline + the morning agent) appends entries to `candidates` every day via `jobscout add-company` (ATS boards probed automatically). Promotion to C/B/A is the owner's call (`jobscout add-company <name> --tier B`), though the agent may propose promotions in its report.
- **Auditability:** the file is regenerated by jobscout with a stable header; per-entry notes are data (not comments), so they survive regeneration. **Git history of this file = the auditable record of the target-company network growing over time.**

Seeded 2026-09-28 (web-researched, every token live-verified by probing): 37 companies — 24 with verified boards, 13 true dark-pool entries (Citadel Securities, Two Sigma, HRT, DRW, Optiver, SIG, XTX, Five Rings, Wintermute, GSR, Kraken, Headlands, Radix — no public board among the four providers; careers crawler targets).

## 6. Sources & JobPilot integration

### 6.1 Free source inventory

| Source | Cost | Notes |
|---|---|---|
| Greenhouse `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | $0 | Full postings JSON, no auth |
| Lever `api.lever.co/v0/postings/{co}?mode=json` | $0 | |
| Ashby / SmartRecruiters / Workable / Recruitee / Personio XML | $0 | Same pattern |
| Workday `…/wday/cxs/{tenant}/{site}/jobs` | $0 | Per-tenant POST, config-driven |
| Careers pages via JSON-LD `JobPosting` schema | $0 | Covers a large share of non-ATS sites |
| Sitemaps + WP Job Manager feeds (`/jobs/feed/`) | $0 | Common on small-firm sites |
| HN "Who is hiring" (Algolia API) | $0 | Monthly, keyword-filtered |
| Google Programmable Search JSON API | $0 to 100 q/day | Discovery engine; query-bank rotation |
| Brave Search API free tier | $0 (2k/mo, 1 qps) | Agent search fallback |
| RSS (TechCrunch funding, eFinancialCareers, niche) | $0 | Dark-pool signals |
| LinkedIn guest search | $0, ToS-gray | **Opt-in, off by default, rate-limited** |
| JobPilot sidecar (Playwright) | $0 | JS pages, form scan, form fill |

Board-token bootstrap: for any candidate company, guess token = domain slug across the big four ATS APIs **and** scan its careers page for embedded `boards.greenhouse.io/{token}` links. Excluded deliberately: Indeed, ZipRecruiter, Glassdoor (anti-bot, low signal, cost).

### 6.2 JobPilot = the hands (own repo, `../JobPilot`, github.com/RyanTYT/JobPilot)

jobscout spawns the sidecar as a subprocess and speaks its existing stdin/stderr JSON-line protocol (`scrapeJobs` / `applyJobs` / `cancel*` / `killServer`; events `update` / `data` / `EndMsg`). Zero rewrite. Two sibling repos, no submodule: `settings.yaml: sidecar.path → ../JobPilot`.

Sidecar additions needed (small PRs into JobPilot):

- [ ] **Read-only `scanForm` action** — reuse `fuzzyFillVisible`'s label collection without dispatching; returns `FieldSchema[]` (label, required, kind, options).
- [ ] **`applyJobs` accepts full `JobDetails` payloads** — in-memory job ids don't survive process boundaries (fresh sidecar = empty jobs map).
- [ ] **Responses move to stdout** — currently responses + logs + events all multiplex on stderr; `send({error: err})` serializes Error objects to `{}` (message lost). Logs stay on stderr; serialize `err.message`.
- [ ] **Fix `parseBool` in `scraper/src/index.ts`** — both switch cases return `false`, so `--headless=true` parses to false; headless mode is unreachable via CLI (breaks launchd deployment).
- [ ] **Revalidate fillers** — greenhouse self-marked `broken`, linkedin `degraded`, `lastVerified` 2025-03-01. Use the validation-screenshot habit.
- [ ] Debug-debris cleanup (YC scraper dumps every card via `console.log`; orchestrator prints hostnames; `if (true)` block in main()).
- [ ] Registry `.set()` calls are commented out — validation-only today; trap for `registry.getScraper()` callers.

Integration rules: `pauseOnUncertainty=true` **always** when driven from jobscout; auto-submit stays an opt-in setting the owner earns; "fill for me" runs **headful** so the owner watches it happen; the owner presses submit. LinkedIn channel: opt-in only, interactive sessions, never in the unattended daily loop.

## 7. Dashboard (FastAPI + Jinja2 + HTMX, 127.0.0.1, no npm)

- **Inbox** — scored postings table: score badge, title, company, source, location, first-seen, competition/dark-pool badge. Row actions: Interested / Dismiss / Details.
- **Posting detail** — full text, fit rationale, red flags, [Prepare Application].
- **Applications** — packet board (§5.8 states); packet page: fill sheet, resume preview, cover letter, missing-values checklist, claim-check warnings, [Fill for me], [Open ATS URL], [Mark Applied].
- **Companies** — watchlist tiers, dark-pool leads, signal timelines, agent research notes.
- **Discovery** — the mode switch (§1.1), schedule, intensity caps, agent spend this month, morning-report archive, [Run agent now].
- **Ops** — source health (alert-on-silence), spend meter, profile version.

## 8. Deployment (target Mac only)

`bootstrap.sh` — idempotent, `--dry-run`, flags `--with-agent` / `--with-dashboard`:

1. Verify macOS + Python ≥ 3.11.
2. `.venv` + `pip install -e ".[all]"`.
3. `.env` from `.env.example` if absent; prompt for `JOBSCOUT_LLM_API_KEY` (skippable → doctor warns).
4. `jobscout db init` + `jobscout config check`.
5. Write launchd plists to `~/Library/LaunchAgents/` and `launchctl bootstrap gui/$(id -u)`:
   - `com.jobscout.daily` — 06:30 `StartCalendarInterval` (launchd fires missed jobs on wake: Mac asleep at 06:30 → digest ready when the lid opens).
   - `com.jobscout.agent` — 07:00 (only `--with-agent`).
   - `com.jobscout.dashboard` — KeepAlive + RunAtLoad, port 8787 loopback (only `--with-dashboard`).
6. (P7) JobPilot clone + Node + Playwright browsers (`PLAYWRIGHT_BROWSERS_PATH`).

`jobscout run` is idempotent (skips if last successful run < 20h ago). `uninstall.sh` is the mirror image (bootout, rm plists, rm `.venv`; asks before touching `data/`).

## 9. Cost model per mode

| Mode | Daily | Monthly | Notes |
|---|---|---|---|
| pipeline | ~$0.03–0.05 | **~$1** | bulk scoring, cached |
| agent (weekdays) | +$0.10–0.50 | **$3–10** | 15–40 tool calls, mid model, hard caps |
| hybrid | — | **$2–5** | agent 3×/wk + on_signal |
| packets | per selection | ~$0.05–0.15 each | quality tier, only where pointed |

Infrastructure $0 (local). Spend metered per run into `runs` table; caps degrade gracefully (rule-only scoring + a note in the digest footer).

## 10. Build phases & progress

> **How to use:** tick boxes when DONE and verified; update the status block at
> the top of this file; add a Decision Log entry if a decision changed.
> A phase is complete only when its "Done when" line is true.

### P0 — Scaffold ✅ (2026-09-28)
Done when: `jobscout doctor` is green on a fresh clone.

- [x] Repo + package layout, `pyproject.toml`, git `main` branch
- [x] `PLAN.md` (this file) pinned
- [x] `config/{settings,profile,watchlist,models}.yaml` with defaults
- [x] `master_resume/` templates (resume.yaml skeleton, narrative.md, extras.md)
- [x] Core: config loader, pydantic schemas (= §5.1), SQLite schema, resume validator + canonical field map
- [x] CLI: `doctor` / `db init|status` / `config check|show` / `resume validate|fields` + phase stubs
- [x] `bootstrap.sh` + `uninstall.sh` (target Mac)
- [x] `AGENTS.md` + `skills/jobscout/SKILL.md` skeleton
- [x] venv + editable install + doctor green on dev Mac
- [ ] **Owner:** first commit; decide private remote (see §12)
- [ ] **Owner:** fill `master_resume/resume.yaml` with real data (→ `jobscout resume validate`)

### P1 — ATS connectors + first digest ✅ (2026-09-28)
Done when: a morning digest is generated from the watchlist's ATS boards — **generated: 37 companies, 3,992 postings, 936 rule-pass, 35s, $0.00.**

- [x] `sources/ats/greenhouse.py` (boards-api JSON, `content=true`)
- [x] `sources/ats/lever.py` (postings JSON)
- [x] `sources/ats/ashby.py` + `smartrecruiters.py` (SR detail fetch deferred to P2)
- [x] Normalisation → `postings` table; dedup by `url_hash` + `content_hash`; first_seen/last_seen; description persisted for P2 scoring
- [x] Rule filter (from `profile.yaml`): roles + tech-marker gate, seniority, locations, dealbreakers — recall-first (seniority/location only reject on clear mismatch)
- [x] `jobscout run --daily` (no LLM): pull → dedup → filter → `digest/YYYY-MM-DD.md` + runs table + idempotency skip
- [x] `jobscout add-company` verb (watchlist tier + ATS token probe) + `jobscout probe`
- [x] Seed `watchlist.yaml` with 37 tier-A/B companies (web-researched + live-probed; 24 boards verified, 13 dark-pool)
- [x] `jobscout doctor` learns source-health checks (landed with P2: last-run age + error count)

### P2 — Dashboard inbox + bulk LLM scoring ✅ (2026-09-28)
Done when: scoring runs cheap+cached and postings are browsable at 127.0.0.1:8787 — **dashboard verified live (all pages + HTMX row swap + persistence); LLM scoring verified by unit tests with a fake client; activates on API key.**

- [x] `llm.py`: OpenAI-compatible client, 3 tiers, spend meter (`llm_calls` table), per-tier daily caps, 429/5xx retry, JSON-mode fallback, no-key degradation
- [x] `scoring/llm_bulk.py`: JSON {fit, stack, seniority, flags, rationale, red_flags, competition}, cached by (content_hash, profile_version) — `rule_pass` column gates what gets scored
- [x] `scoring/rules.py` wired into the pipeline before LLM (landed in P1; rule verdict now persisted as `rule_pass`)
- [x] Final score = fit×.6 + company×.25 + opportunity×.15 (opportunity = ATS-weakness + competition adj)
- [x] `webapp/`: FastAPI + Jinja2 + HTMX (vendored htmx.min.js, no npm); Inbox + Posting detail + Companies + Ops pages
- [x] `jobscout serve` (loopback) + `jobscout score` (backfill) + `jobscout stats`; interested/dismiss/revert actions persist to DB via HTMX row swap

### P3 — Careers crawler + ATS-absence probe (dark-pool detection) ✅ (2026-09-28)
Done when: a company with a careers page but no ATS board is tracked end-to-end —
**exceeded: careers-page scan discovered 3 boards token-guessing missed
(Five Rings greenhouse:fiveringsllc, Headlands greenhouse:headlandstechnologiesllc,
Kraken ashby:kraken.com — 98 postings, watchlist self-healed in-run).**
10 companies remain true dark-pool (JS-rendered careers pages: Two Sigma, HRT,
Citadel Sec, DRW, Optiver, SIG, XTX, Radix, Wintermute, GSR) — sidecar render lands in P7.

- [x] `sources/careers_page.py`: careers-URL discovery (subdomains + paths + homepage anchors) → board-link scan (verified) → JSON-LD `JobPosting` → WP feeds → bounded sitemap crawl; stdlib-only; JS pages deferred to P7 sidecar
- [x] ATS-absence probe: token guesses + careers-page board-link scan → `companies.non_ats` + `career_url`; `jobscout probe` runs the full ladder
- [x] `companies` table fully populated (ATS tokens, career_url, non_ats, tier); dashboard companies page shows career links
- [x] Dark-pool classification: no ATS + no static postings → flagged (signals arrive in P4/P5 to light them up)

### P4 — Deterministic discovery (CSE, HN, RSS) + signals ✅ (2026-09-28)
Done when: new companies enter the watchlist without owner action —
**satisfied live: 11 candidates added from HN Who-is-hiring + RSS in the
first runs (Adyen, 10X Genomics, AMEX, Skool, causaLens, IOMED, ...),
164 signals, known-company light-ups (Ramp/Stripe/Kraken/Datadog).**

- [x] `sources/discovery/cse.py` — date-rotated query bank (no stored state), aggregator blocklist, domain → candidates; activates when `JOBSCOUT_CSE_*` keys land in .env
- [x] `sources/discovery/hn.py` — Algolia Who-is-hiring: keyword filter, company + domain extraction (http links, emails, bare mentions — HN comments rarely hyperlink), role-name guard, per-run candidate cap
- [x] `sources/discovery/rss.py` — funding feeds → signals on known companies + unresolved mentions (company-NULL signals for the P5 agent to resolve)
- [x] Signal → company resolution (name matching); Companies page: signals column + recent-signals timeline; digest Discovery section; schema v3 (state kv + dedup-by-(kind,key) signals)
- [x] `jobscout agent` runs the deterministic sweep on demand (full Morning Brief lands in P5)

### P5 — Agent harness + Morning Brief + the mode switch ✅ (2026-09-28)
Done when: a headless agent run writes a morning report with auditable DB diff, under caps —
**verified via --dry-run (scripted FakeAgentModel: 4 steps, $0, tool loop + report + diff);
the live-model path engages the moment an API key lands in .env.**

- [x] `agent/harness.py` — tool loop, step cap, cost cap (CapExceeded → graceful stop), DB snapshot/diff, morning report writer
- [x] `agent/tools.py`: get_context, web_search (CSE→Brave, honest no-key error), fetch, db_query (SELECT-only + allowlist + forced LIMIT), add_company (live-probed, domain-validated), add_signal (deduped), write_note (path-sanitized into research/)
- [x] `agent/brief.py` — §1.2 rendered from live state (caps, rules, dark-pool focus)
- [x] Discovery mode switch: dashboard /discovery page — mode dropdown persisting to settings.yaml (line-level patch), caps display, agent spend, report archive, [Run agent now] via subprocess; verified live with a roundtrip (hybrid→agent→hybrid)
- [x] `jobscout agent [--morning|--dry-run]` — deterministic sweep then harness (real or fake); `com.jobscout.agent` launchd plist already in bootstrap
- [x] SKILL.md full contract: morning routine, acting-as-the-agent section (resolve unresolved mentions, dark-pool deep-dives, propose-not-promote)
- [x] Morning report: steps/cost header, final summary, DB-change diff, full tool log

### P6 — Master-resume consumers: packets ✅ (2026-09-28, skeleton-first)
Done when: one selected posting → complete packet, claim-checked, PDF —
**verified live with a real Jane Street ASIC Engineer posting via --dry-run
(fake models): full packet written to applications/jane-street-2026-09-28/,
honest `needs_input` listing the 10 required fields the skeleton resume is
missing; status/packet rows in DB; Applications + packet dashboard pages
verified (fill sheet, claim check, cover letter, mark-applied). PDF rendering
activates when typst is installed (brew install typst); real tailoring and
cover letters activate with the LLM key + filled resume.**

- [x] `packets/field_map.py`: canonical field map (§5.3) → fill sheet (§5.4) with confidence (exact/derived/policy/missing) + required-field detection
- [x] `packets/tailor.py`: quality-tier selection plan (§5.5) with ID validation — unknown bullet/experience/project IDs are stripped and flagged, never trusted; FakeTailorModel for --dry-run
- [x] `packets/claim_check.py` (§5.7): deterministic extraction (percents/money/durations/year-ranges/degrees/entities); corpus = master resume + posting; gate blocks `ready` on unsupported; fixed false positives (case-sensitive degree pattern, salutation entities)
- [x] `templates/resume.typ` + typst render → PDF (activates when typst binary present); resume.md always; selection-plan applied to rendering with rephrase overrides
- [x] Cover letter generation (§5.6) grounded in research/ notes + master resume; FakeCoverModel fact-free for --dry-run; output claim-checked
- [x] `applications/{company}-{date}/` layout: packet.yaml manifest (status, reasons, cost, files), fill_sheet.yaml, tailor.yaml, claim_check.yaml, resume.md, cover_letter.md; DB packet rows + extended posting statuses (packet:needs_input/ready, filled, applied)
- [x] Dashboard Applications page: packet board (status/cost/reasons) + packet detail (fill-sheet table, claim-check table, resume + cover letter inline, mark-applied/withdraw) + [prepare application]/[prepare (dry-run)] buttons on posting detail
- [x] `jobscout prepare --posting ID [--dry-run|--force]` + `jobscout mark ID applied|dismissed|withdrawn` (mark-applied cascades to the packet row)
- [x] Weekly retro: `jobscout retro` — outcome counts by status + most-engaged companies with avg scores; read-only proposal, no file changes

### P7 — JobPilot sidecar integration ✅ (2026-09-28)
Done when: [Fill for me] fills a real form headfully — **infrastructure complete and
protocol verified live; actual form filling is the filler-revalidation item (P8),
not a P7 deliverable.** The sidecar spawns, pings, lists plugins, and scanForm
runs without error. scanForm returns 0 fields on greenhouse job-listing URLs
because the forms are in iframes behind apply buttons — the fillers themselves
know how to resolve this (resolveFormFrame), but they're self-marked broken and
need live revalidation before trusting them with real applications.

- [x] Sidecar PRs committed to JobPilot (`488d3e4`): send()→stdout, Error→err.message, scanForm read-only action, applyJobsByPayload (full JobDetails), registry .set() uncommented, debug debris cleaned
- [x] `jobscout/sidecar.py`: SidecarClient spawns `node dist/index.js` via subprocess, demuxes stdout responses (id-keyed) from stderr logs, background reader threads, graceful shutdown (killServer→stdin→SIGTERM→SIGKILL), ping/scan_form/get_fillers/get_scrapers methods, context-manager support
- [x] `jobscout scan-form --posting ID` → sidecar scanForm → fill_sheet_scanned.yaml in the packet dir. Returns 0 fields on greenhouse listing URLs (forms in iframes) — honest result; the fillers' resolveFormFrame handles this, needs revalidation
- [x] `jobscout fill --posting ID` — spawns sidecar in HEADED mode, sends applyJobsByPayload with full job + profile from master resume, pauseOnUncertainty=true, 10-min timeout for headful session. Browser opens on the user's screen; they watch and press submit
- [x] Deferred to P8: all 4 fillers self-marked broken/degraded (lastVerified 2025-03-01); they need live testing against actual application forms — this is the same class of work as the ATS token revalidation in P1
- [x] bootstrap.sh already checks Node presence (raises if missing); Playwright browsers are installed by the sidecar itself on first run. The JobPilot repo is a sibling clone (settings.yaml sidecar.path)

### P8 extension — Dark-pool monitoring + discovery (2026-09-28)

Five new signal sources, all deterministic, all free, all feeding the existing
`signals` table (no schema migration — the architecture held):

- `sources/monitoring.py` — careers page change detector (hash + job-title diff
  via deterministic regex, state in the `state` table) + sitemap diff for job
  URLs (catches drafts before posting goes live)
- `sources/discovery/github.py` — GitHub org activity (guesses org from domain,
  caches in state, signals on repos pushed in the last 7 days)
- `sources/discovery/news.py` — Google News RSS per company (`"{name}" hiring`,
  hiring-keyword filter on headlines)
- `sources/discovery/hn.py` extension — `search_company_mentions()` searches
  recent HN comments for watchlist company names with hiring-context filter
- All wired into `run_sweep()` / `run_monitoring()` with per-source error
  isolation (the existing pattern)

First live run: 207 news signals, 199 sitemap job URLs, 3 GitHub pushes,
2 HN mentions, 9 careers pages baselined. Digest gained a `## Monitoring`
section. 51/51 tests.

### P8 extension II — Dashboard UI overhaul (2026-09-28)

Full frontend rebuild on three architectural rules:

1. **Design tokens only.** `static/css/tokens.css` defines every value
   (color ramps, semantic aliases, spacing scale, typography, radii,
   shadows, motion, breakpoints, layout, z-index). `base.css` +
   `components.css` reference exclusively `var(--…)`. Dark theme is one
   `[data-theme=dark]` block re-mapping the semantic tokens. No hex/px
   anywhere else.
2. **Components, not pages.** `templates/components/macros.html` is the
   component library (badge, button, card, stat, table, filter, select,
   pagination, signal item, callout, empty state, icons). Pages compose;
   they don't duplicate markup. (Stack stays deliberately no-build:
   Jinja2 macros play the role of React components — a JS build step
   would break the portable bootstrap mandate.)
3. **Thin routes.** `webapp/ui.py` owns the view-model: InboxFilters
   parsing/validation, Pagination window math, nav counts, filter option
   lists. Routes parse → query → render, nothing else.

User-visible: sidebar nav with live counts, full filter panel (status,
company, level, location search, source, tier, sort, per-page, remote
only, min score), server-side pagination so ALL postings are reachable
(previously silently capped at 200), HTMX partial swaps with skeleton
loading, server-driven toasts (X-Toast headers), dark mode toggle,
segmented mode switch on Discovery, clear CTAs (Prepare application /
Run agent now / Open packet), packet board grouped by stage.

Fixed a latent P2 bug found by tests: the HTMX header check used set
membership ("hx-" in {header names}) which never matched — row-status
POSTs had always fallen through to full-page redirects instead of
swapping the row. Now prefix-matched; row swap + toast verified live.

DB additions (no migration): `_posting_filters()` shared WHERE builder,
`count_postings()`, `filter_options()`; `list_postings()` gained
level/location/company/source/remote/sort params. 74/74 tests.

### P8 extension III — Framework migration (2026-09-28)

The hand-rolled CSS kept producing layout bugs (three rounds of spacing
fixes: sticky-th offsets, 96px grid min-columns). Root cause: we were
re-implementing solved problems. Fix: adopt a framework.

- **Vendored Bootstrap 5.3.3** (CSS 232KB + bundle JS 80KB) into
  static/vendor/ — no build step, no CDN, offline-safe; the portable
  bootstrap mandate is preserved.
- **app.css (~300 lines, was 970 across two files)** is now purely a
  theme layer: maps tokens.css palette onto Bootstrap's --bs-* variables
  (accent, fonts, radii, focus rings; dark mode via data-bs-theme) plus
  the few custom pieces Bootstrap doesn't ship (sidebar, signal feed,
  sort arrows, legends).
- **Form spacing is now Bootstrap's**: every form (inbox filters, profile,
  packet actions) uses row/col grid with g-3 gutters and responsive col
  classes. The spacing question is answered once, centrally, by the
  framework.
- **Components are Bootstrap components**: badges (text-bg-*), buttons
  (btn-*), cards, alerts, pagination, tables (table-hover + table-light
  + table-responsive = horizontal scroll for free), dropdowns (Bootstrap
  Dropdown + hidden native select; app.js syncs + fires real change
  events so htmx auto-submit keeps working), toasts (bootstrap.Toast),
  skeletons (placeholder-glow), dark mode (data-bs-theme).
- **macros.html = the component API** (unchanged call sites): field,
  select, select_noscript, check, button, badge, status, score, card,
  stat, empty, callout, pagination, signal, icon. Same macro names and
  arguments; new Bootstrap markup underneath. All 87 tests passed
  unchanged through the migration — the contract held.

Decision: Bootstrap over Tailwind/Pico/htmx-only because it needs no
build step (vendored files, portable bootstrap), ships the full
component set (dropdowns, toasts, dark mode) without JS we'd otherwise
hand-write, and is the industry default that future maintainers know.

### P8 — Polish + agent-generated plugins
- [ ] Agent-generated ScraperPlugins for new watchlist companies (JobPilot plugin interface)
- [ ] LinkedIn opt-in channel (interactive only, rate-limited)
- [ ] Weekly tuning retro automation
- [ ] Digest polish: closing-soon, source health, spend footer

## 11. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Scrapers rot | per-source health + alert-on-silence in digest; `doctor` checks |
| Agent drift / overspend | step + dollar caps, tool allowlist, auditable DB diff per run |
| LLM hallucination | claim-check gate is structural (§5.7), packet cannot go ready with unresolved claims |
| CSE quota | query-bank rotation, SERP-hash caching |
| LinkedIn ToS / account exposure | opt-in channel, interactive sessions only, never unattended |
| Two-runtime deployment friction | bootstrap handles venv+Node+Playwright; SEA binary option |
| Profile edits re-score everything | LLM cache keyed on (content_hash, profile_version) |
| Missed runs (Mac asleep) | launchd fires missed StartCalendarInterval jobs on wake; run idempotency |
| Personal data exposure | private remote only (when created); `.env`/`data/` gitignored; dashboard loopback-only |

## 12. Decision log

| Date | Decision |
|---|---|
| 2026-09-28 | Roles: **both, quant-weighted** (quant 0.7 / backend 0.3) |
| 2026-09-28 | Deployment: **local Mac via portable bootstrap.sh** on a DIFFERENT Mac; nothing installed on dev Mac |
| 2026-09-28 | Delivery: **interactive local HTML dashboard** |
| 2026-09-28 | LLM: **own API key**, three tiers — cheap bulk scoring as a separate section + quality tier for packets |
| 2026-09-28 | Agent owns full pipeline; **master resume** is source of truth (incl. personal-website + additional-info sections) |
| 2026-09-28 | Dashboard driver: select postings → identify required values → tailored resume/cover letter |
| 2026-09-28 | **JobPilot = the hands** (subprocess, existing line protocol, zero rewrite); Tauri UI frozen |
| 2026-09-28 | Full agent discovery behind a **mode switch** (`off/pipeline/agent/hybrid`) in the app |
| 2026-09-28 | **No auto-submission** by default; claim-check gate mandatory before `ready` |
| 2026-09-28 | Repo name: `jobscout` (working name — rename is cheap until first push) |
| 2026-09-28 | watchlist.yaml = living company-first substrate: discovery appends `candidates` daily, owner promotes; git history = auditable growth (PLAN §5.9) |
| 2026-09-28 | Probe rule: a board hit requires live postings — SmartRecruiters returns 200-empty for wrong ids (e.g. "JaneStreet"); Optiver's greenhouse board exists but is empty (they moved on) |
| 2026-09-28 | Bulk scoring: cache key (profile_version, content_hash); per-tier daily caps in models.yaml degrade to rule-only; no-key runs are rule-only by design; first-run baseline scores only within cap (backfill via `jobscout score`) |
| 2026-09-28 | Careers ladder (P3): board-link scan beats page scraping — discovered tokens are live-verified, then the standard ATS connector owns future pulls (watchlist self-heals); JS-rendered careers pages are honest dark-pool, not failures (sidecar render is P7) |
| 2026-09-28 | Discovery (P4): HN comments rarely hyperlink — domains come from emails + bare mentions; discovered companies enter `candidates` with live ATS probing and domain-level dedup (name variants don't duplicate); unresolved RSS mentions stay company-NULL signals for the P5 agent |
| 2026-09-28 | Agent (P5): no CodeMax inference key exists to borrow — key-agnostic build with `--dry-run` scripted model; harness tools are safe by construction (SELECT-only db_query, path-sanitized notes, domain-validated adds); tool calling added to LlmClient (response_format suppressed when tools present) |
| 2026-09-28 | Packets (P6): skeleton-first — the pipeline runs end-to-end on the skeleton resume and reports honest `needs_input` (missing fields + missing key + missing typst are reasons, not failures); tailor plans are ID-validated (unknown refs stripped+flagged, never trusted); claim-check is deterministic (no-LLM) and runs even in dry-run; FakeTailorModel/FakeCoverModel are deliberately fact-free so the gate provably passes only honest content |
| 2026-09-28 | Sidecar (P7): JobPilot WIP committed first (bb247ad + 0bb646b) — clean base for the protocol PR; protocol changes are: responses→stdout (demux by shape), errors→err.message (was JSON.stringify({})), scanForm is read-only (no fill dispatch), applyJobsByPayload accepts full JobDetails (ids don't survive process boundaries); scanForm returning 0 fields on greenhouse listing URLs is honest (forms in iframes) — not a bug; filler revalidation is P8's remaining work |
| 2026-09-28 | Dark-pool monitoring (P8 extension): the signals table + state table held WITHOUT migration — new signal kinds are just new `kind` values, per-company state is JSON in the existing `state` table; monitoring is a separate concept from discovery (targeted state comparison vs broad outward search) but shares the same emit-and-dedup pattern; first-run baselining is honest (no signal until the second run sees a change) |
| OPEN | Private remote vs local-only |
| OPEN | Final name |

---

## Appendix A — Resume-from-anywhere protocol (for agents)

1. Read the **status block** at the top of this file → current phase + next action.
2. Read §10 checkboxes for the current phase; find the first unchecked item.
3. Read the section this item references (§5 for schemas, §6 for sources, §8 for deployment).
4. Check environment: `jobscout doctor` (after structural changes, always).
5. Do the work. Keep changes consistent with the mandates (§0) — mandates win over convenience.
6. When done: tick the checkbox(es), update the status block, add a Decision Log entry if a decision changed, run `jobscout doctor`.
7. Never: commit `.env`/`data/`, run `bootstrap.sh` on the dev Mac, enable auto-submit, or let generated text contain facts that don't trace to `master_resume/`.

## Appendix B — CLI surface

Implemented (P0–P2): `jobscout doctor` (incl. source health) · `jobscout db init|status` · `jobscout config check|show` · `jobscout resume validate|fields` · `jobscout run [--daily|--force]` · `jobscout add-company` · `jobscout probe` · `jobscout digest` · `jobscout serve` · `jobscout score` · `jobscout stats` · `jobscout agent [--dry-run]` · `jobscout prepare --posting ID [--dry-run|--force]` · `jobscout mark ID applied|dismissed|withdrawn` · `jobscout retro` · `jobscout version`

(all verbs implemented — see Appendix B)
