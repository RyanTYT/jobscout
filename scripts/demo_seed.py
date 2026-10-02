"""Build the synthetic demo runtime behind the README's screenshots + videos.

The published assets in docs/ are captured from a THROWAWAY runtime, not from
a real job hunt: a fictional company graph, fictional postings, and a
fictional candidate. Nothing here touches the live config/, master_resume/ or
var/ — the whole runtime lives under the target directory and is reached only
via JOBSCOUT_HOME.

Everything synthetic passes through the real code paths on purpose, so the
demo cannot drift from the product:
  * scores come from scoring.llm_bulk.compute_final
  * the claim-check table is produced by packets.claim_check.check_packet,
    and each packet's status is derived from that gate's own verdict
  * packet manifests, fill sheets and reports are written in the shapes the
    templates and jobscout.webapp.reports actually read

Usage:
    .venv/bin/python scripts/demo_seed.py --home /tmp/jobscout-demo
    JOBSCOUT_HOME=/tmp/jobscout-demo .venv/bin/jobscout serve --port 8801

Then capture with any browser driver. Nothing here reaches the network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402
from rich.console import Console  # noqa: E402

# ── the fictional cast ───────────────────────────────────────────────────────

CANDIDATE = {
    "name": "Avery Nakamura",
    "email": "avery@example.dev",
    "phone": "+65 0000 0000",
    "city": "Singapore",
    "country": "Singapore",
    "website": "https://example.dev",
    "linkedin": "https://linkedin.example/in/avery",
    "github": "https://github.example/avery",
    "current_company": "Tessera Data Labs",
    "current_title": "Software Engineer, Market Data",
    "school": "National University of Singapore",
    "degree": "BSc Computer Science",
}

# (name, domain, ats, note)  — ats={} means NO board: a dark-pool entry
TIERS: dict[str, list[tuple[str, str, dict, str]]] = {
    "A": [
        ("Meridian Quant Partners", "meridianquant.example", {"greenhouse": "meridianquant"},
         "cross-venue execution in Rust + C++; own careers page — dark-pool entry"),
        ("Halcyon Markets", "halcyonmarkets.example", {"lever": "halcyon-markets"},
         "market making, Singapore + HK desks"),
        ("Northlake Trading Systems", "northlaketrading.example", {"ashby": "northlake"},
         "exchange connectivity; FPGA adjacent but mostly software"),
        ("Cobalt Exchange Systems", "cobaltexchange.example", {"greenhouse": "cobalt-exchange"},
         "matching engine + risk, low-latency C++/Rust"),
        ("Vantage Point Markets", "vantagepoint.example", {},
         "no public ATS board — dark-pool entry, hiring signal in Q3 news"),
        ("Kestrel Financial Systems", "kestrelfs.example", {},
         "no public ATS board — dark-pool entry; careers page changed 2026-09-19"),
    ],
    "B": [
        ("Tessera Data Labs", "tessera.example", {"lever": "tessera-data"},
         "market-data pipelines, Kafka + Rust"),
        ("Sable Ridge Trading", "sableridge.example", {"greenhouse": "sableridge"},
         "commodities + rates execution"),
        ("Onyx Clearing", "onyxclearing.example", {},
         "post-trade / clearing infrastructure; no board"),
        ("Lumen Order Flow", "lumenorderflow.example", {"lever": "lumen-order"},
         "order flow analytics; small team, real ownership"),
        ("Pinewood Market Systems", "pinewoodms.example", {},
         "surveillance tooling; no board"),
    ],
    "C": [
        ("Bramble Quant", "bramblequant.example", {},
         "small prop shop, off-cycle hiring"),
        ("Ferrier Digital Markets", "ferrierdm.example", {"ashby": "ferrier"},
         "retail CFD platform; product-heavy"),
        ("Granite Bay Analytics", "granitebay.example", {},
         "data consultancy serving funds; no board"),
    ],
}

CANDIDATES: list[tuple[str, str, str]] = [
    ("Aurora Loop Systems", "auroraloop.example", "blog"),
    ("Cobalt Ridge Data", "cobaltridge.example", "hn"),
    ("Driftwood Markets", "driftwoodmk.example", "news"),
    ("Eastvale Quant Labs", "eastvale.example", "hn"),
    ("Flintpoint Systems", "flintpoint.example", "github"),
    ("Gantry Row Trading", "gantryrow.example", "funding"),
    ("Hollowbrook Markets", "hollowbrook.example", "monitoring"),
    ("Ironvale Data", "ironvale.example", "news"),
    ("Juniper Fields Analytics", "juniperfields.example", "search"),
    ("Kittiwake Markets", "kittiwake.example", "search"),
]

SIGNAL_NOTES = [
    ("Vantage Point Markets", "news",
     'Newswire: "Vantage Point expands Singapore trading desk"'),
    ("Kestrel Financial Systems", "careers_change",
     "careers page changed: +2 engineering roles"),
    ("Aurora Loop Systems", "blog",
     'Engineering blog: "Why we are hiring" post'),
    ("Cobalt Ridge Data", "hn", "HN thread: order book replay tooling"),
    ("Driftwood Markets", "funding", "Funding database hit: seed round"),
    ("Eastvale Quant Labs", "hn", "HN mention by an engineer, no careers page yet"),
    ("Flintpoint Systems", "github", "GitHub org: jobs advertised in README"),
    ("Gantry Row Trading", "funding", "Funding database hit: Singapore entity"),
    ("Hollowbrook Markets", "careers_change", "sitemap diff: /careers appeared this week"),
    ("Ironvale Data", "news", 'Newswire: "Ironvale Data expands APAC hiring"'),
    ("Juniper Fields Analytics", "search", "CSE hit: conference speaker list, unclaimed"),
    ("Kittiwake Markets", "search", "CSE hit: domain could not be verified"),
    ("Onyx Clearing", "careers_change", "careers page changed: 1 new role"),
    ("Sable Ridge Trading", "news", 'Newswire: "Sable Ridge opens Singapore rates desk"'),
    ("Halcyon Markets", "funding", "Funding database hit: bridge round"),
    ("Lumen Order Flow", "blog", "Changelog: shipped a replay API, mentions headcount"),
    ("Pinewood Market Systems", "careers_change", "careers page changed: 1 role closed"),
    ("Meridian Quant Partners", "news", "Newswire: Meridian opens Amsterdam execution desk"),
    ("Northlake Trading Systems", "github", "GitHub: protocol library v2 released"),
    ("Cobalt Exchange Systems", "github", "GitHub: hiring in a public issue thread"),
]

# (company, title, location, seniority, source, fit 0-1, opportunity 0-1, days_ago, status)
# fit/opportunity are fed through compute_final, which derives company_quality
# from the tier and opportunity from ATS absence + the competition grade.
POSTINGS: list[tuple] = [
    ("Cobalt Exchange Systems", "Low-Latency Systems Engineer", "Singapore", None, "ats:greenhouse:cobalt-exchange", 0.94, 0.82, 0.4, "new"),
    ("Cobalt Exchange Systems", "Matching Engine Engineer (C++/Rust)", "Singapore", "mid", "ats:greenhouse:cobalt-exchange", 0.88, 0.74, 3.1, "interested"),
    ("Cobalt Exchange Systems", "Site Reliability Engineer", "Singapore", "mid", "ats:greenhouse:cobalt-exchange", 0.61, 0.55, 5.4, "new"),
    ("Cobalt Exchange Systems", "Risk Systems Developer", "Hong Kong", None, "ats:greenhouse:cobalt-exchange", 0.72, 0.70, 8.0, "new"),
    ("Meridian Quant Partners", "Quant Developer — Execution", "Singapore", "junior", "ats:greenhouse:meridianquant", 0.91, 0.78, 1.2, "new"),
    ("Meridian Quant Partners", "Rust Engineer, Market Connectivity", "Singapore", "mid", "ats:greenhouse:meridianquant", 0.89, 0.71, 2.3, "new"),
    ("Meridian Quant Partners", "Quantitative Researcher", "Singapore", "senior", "ats:greenhouse:meridianquant", 0.48, 0.60, 6.7, "dismissed"),
    ("Meridian Quant Partners", "FPGA Engineer", "Amsterdam", "mid", "ats:greenhouse:meridianquant", 0.44, 0.66, 9.2, "new"),
    ("Halcyon Markets", "Backend Engineer, Market Data", "Singapore", "junior", "ats:lever:halcyon-markets", 0.86, 0.76, 0.8, "new"),
    ("Halcyon Markets", "Trading Systems Engineer", "Hong Kong", "mid", "ats:lever:halcyon-markets", 0.90, 0.73, 2.9, "new"),
    ("Halcyon Markets", "Software Engineer, Execution", "Singapore", None, "ats:lever:halcyon-markets", 0.84, 0.80, 4.6, "new"),
    ("Northlake Trading Systems", "Connectivity Engineer", "Singapore", "junior", "ats:ashby:northlake", 0.83, 0.85, 1.6, "new"),
    ("Northlake Trading Systems", "Backend Engineer, Order Routing", "Amsterdam", "mid", "ats:ashby:northlake", 0.81, 0.69, 7.3, "new"),
    ("Northlake Trading Systems", "Exchange Protocol Specialist", "Singapore", "senior", "ats:ashby:northlake", 0.55, 0.64, 11.0, "new"),
    ("Vantage Point Markets", "Software Engineer", "Singapore", None, "careers:vantagepoint.example", 0.82, 0.92, 0.2, "new"),
    ("Vantage Point Markets", "Infrastructure Engineer", "Singapore", "mid", "careers:vantagepoint.example", 0.74, 0.90, 5.9, "new"),
    ("Kestrel Financial Systems", "Backend Engineer, Post-Trade", "Singapore", "junior", "careers:kestrelfs.example", 0.79, 0.94, 1.1, "new"),
    ("Kestrel Financial Systems", "Data Platform Engineer", "Singapore", "mid", "careers:kestrelfs.example", 0.76, 0.91, 4.2, "new"),
    ("Tessera Data Labs", "Senior Backend Engineer", "Singapore", "senior", "ats:lever:tessera-data", 0.58, 0.55, 0.6, "dismissed"),
    ("Tessera Data Labs", "Data Engineer", "Singapore", "mid", "ats:lever:tessera-data", 0.68, 0.51, 3.7, "new"),
    ("Onyx Clearing", "Backend Engineer, Settlement", "Singapore", None, "careers:onyxclearing.example", 0.77, 0.89, 2.1, "new"),
    ("Onyx Clearing", "Platform Engineer", "Singapore", "mid", "careers:onyxclearing.example", 0.71, 0.88, 6.1, "new"),
    ("Lumen Order Flow", "Founding Backend Engineer", "Singapore", "mid", "ats:lever:lumen-order", 0.85, 0.83, 1.4, "new"),
    ("Lumen Order Flow", "Data Platform Engineer", "Singapore", "junior", "ats:lever:lumen-order", 0.80, 0.80, 5.4, "new"),
    ("Sable Ridge Trading", "Software Engineer, Rates", "Hong Kong", "junior", "ats:greenhouse:sableridge", 0.75, 0.72, 2.6, "new"),
    ("Sable Ridge Trading", "Software Engineer, Commodities", "Singapore", "junior", "ats:greenhouse:sableridge", 0.76, 0.72, 3.4, "new"),
    ("Pinewood Market Systems", "Backend Engineer", "Singapore", None, "careers:pinewoodms.example", 0.70, 0.86, 1.9, "new"),
    ("Pinewood Market Systems", "Platform Engineer", "Singapore", "mid", "careers:pinewoodms.example", 0.66, 0.85, 6.8, "new"),
    ("Pinewood Market Systems", "QA Automation Engineer", "Singapore", "junior", "careers:pinewoodms.example", 0.42, 0.72, 8.9, "new"),
    ("Bramble Quant", "Junior Quant Developer", "Singapore", "junior", "careers:bramblequant.example", 0.73, 0.81, 4.4, "new"),
    ("Bramble Quant", "Research Engineer", "Singapore", "mid", "careers:bramblequant.example", 0.60, 0.79, 7.7, "new"),
    ("Ferrier Digital Markets", "Backend Engineer, Platform", "Amsterdam", "mid", "ats:ashby:ferrier", 0.64, 0.68, 2.8, "new"),
    ("Ferrier Digital Markets", "Product Engineer", "Amsterdam", "junior", "ats:ashby:ferrier", 0.56, 0.66, 5.2, "new"),
    ("Granite Bay Analytics", "Backend Engineer", "New York", "mid", "careers:granitebay.example", 0.52, 0.84, 1.7, "new"),
    ("Granite Bay Analytics", "Consultant", "New York", "senior", "careers:granitebay.example", 0.34, 0.71, 6.4, "new"),
    ("Aurora Loop Systems", "Software Engineer, Market Infrastructure", "Singapore", "junior", "site:mycareersfuture:aurora-loop-systems", 0.87, 0.78, 0.3, "new"),
    ("Cobalt Ridge Data", "Backend Engineer", "Singapore", "junior", "site:mycareersfuture:cobalt-ridge-data", 0.78, 0.75, 1.0, "new"),
    ("Driftwood Markets", "Quantitative Developer", "Singapore", "junior", "site:mycareersfuture:driftwood-markets", 0.83, 0.77, 2.0, "new"),
    ("Eastvale Quant Labs", "Systems Engineer", "Singapore", None, "site:mycareersfuture:eastvale-quant-labs", 0.74, 0.76, 3.0, "new"),
    ("Hollowbrook Markets", "Data Engineer", "Singapore", "mid", "site:mycareersfuture:hollowbrook-markets", 0.69, 0.74, 4.0, "new"),
    ("Ironvale Data", "Platform Engineer", "Singapore", "junior", "site:mycareersfuture:ironvale-data", 0.72, 0.73, 5.0, "new"),
    ("Kittiwake Markets", "Backend Developer", "Hong Kong", "junior", "site:mycareersfuture:kittiwake-markets", 0.71, 0.72, 6.0, "new"),
    ("Juniper Fields Analytics", "Engineer, Data Platform", "Singapore", "mid", "site:mycareersfuture:juniper-fields", 0.67, 0.71, 7.0, "new"),
    ("Gantry Row Trading", "Software Engineer", "Singapore", None, "site:mycareersfuture:gantry-row", 0.70, 0.70, 8.0, "new"),
    ("Flintpoint Systems", "Infrastructure Engineer", "Singapore", "junior", "site:mycareersfuture:flintpoint", 0.62, 0.69, 9.0, "new"),
    # rule-excluded by the deterministic gate (senior/lead, wrong location,
    # clearance, on-call) — they only appear with "show rule-excluded" on
    ("Cobalt Exchange Systems", "Head of Engineering", "Singapore", "lead", "ats:greenhouse:cobalt-exchange", None, None, 2.0, "new"),
    ("Meridian Quant Partners", "Senior Compliance Officer", "Singapore", "senior", "ats:greenhouse:meridianquant", None, None, 3.0, "new"),
    ("Halcyon Markets", "Desk Operator (US hours)", "New York", None, "ats:lever:halcyon-markets", None, None, 4.0, "new"),
    ("Halcyon Markets", "Graduate Trader — London", "London", "junior", "ats:lever:halcyon-markets", None, None, 5.0, "new"),
    ("Tessera Data Labs", "Principal Engineer", "Singapore", "principal", "ats:lever:tessera-data", None, None, 6.0, "new"),
    ("Onyx Clearing", "On-Call SRE (24x7)", "Sydney", "mid", "careers:onyxclearing.example", None, None, 7.0, "new"),
    ("Sable Ridge Trading", "Clearance-Required Backend Engineer", "Singapore", "mid", "ats:greenhouse:sableridge", None, None, 8.0, "new"),
    ("Lumen Order Flow", "Security Engineer (SOC-monitored)", "Singapore", "mid", "ats:lever:lumen-order", None, None, 9.0, "new"),
    ("Granite Bay Analytics", "Quant Dev — New Grad", "New York", "junior", "careers:granitebay.example", None, None, 10.0, "new"),
    ("Northlake Trading Systems", "Machine Learning Engineer", "Amsterdam", "mid", "ats:ashby:northlake", None, None, 11.0, "new"),
]

HANDWRITTEN_DESCRIPTIONS = {
    "Low-Latency Systems Engineer": """Cobalt Exchange Systems runs a matching engine and pre-trade risk stack for Singapore and Hong Kong venues. You will own the systems that sit between the exchange gateways and the matching engine: order validation, throttling, and the order-state fan-out our clients read from.

What you will actually do:
- Own the FIX/ITCH gateway layer written in Rust, keeping round-trip times in the low microseconds across a multi-venue topology.
- Extend our pre-trade risk checks, which are currently a mix of Rust and a Python control plane.
- Keep p99 measurable: we have a continuous latency dashboard and a hard budget per hop.

What we look for: production Rust or modern C++, comfort with a network-bound system that you have actually profiled, and enough Python to glue things together. Market-structure knowledge is a plus, not a requirement.""",
    "Quant Developer — Execution": """Meridian Quant Partners is hiring a quant developer onto the execution team. You will build and maintain the signal-to-order path: intent generation, sizing, and the routing decisions that get an order to a venue.

- Rust for the hot path, Python for research glue, Kafka between them.
- You will own execution quality end to end and be measured on it.
- A small team; you will not be one of forty people touching the same service.

We hire people who have shipped a system that had to stay up at 3am. Tell us what you built and what broke.""",
    "Backend Engineer, Market Data": """Halcyon Markets is looking for a backend engineer to join the market-data team in Singapore. Our feeds are messy: five venues, three timestamp conventions, and a corporate-actions feed that is correct exactly twice a day.

- Own the normalisation and reference-data path that everything else depends on.
- Work in Python and Rust; the hot paths are already Rust, the control plane is not.
- Design pressure is real: we are moving from per-service copies of corporate actions to a single authoritative store.

Junior or mid-level. If you have owned a data pipeline that other teams depended on, that is the story we want to hear.""",
    "Software Engineer": """Vantage Point Markets is a market maker with no public ATS board and no third-party recruiter. We hire slowly and keep people a long time.

This role is on the trading-systems side: order state, position keeping, and the plumbing between them. Stack is Rust and Python on Linux, with a lot of custom low-latency work.

Apply through our own careers page — we read everything that comes in and we reply to every application, including the ones we decline.""",
    "Connectivity Engineer": """Northlake Trading Systems builds the connectivity layer for several regional exchanges. As a connectivity engineer you will own sessions to venues you actually have to keep up: drop detection, recovery, sequence-number handling, and the tooling that lets the rest of the firm see that state.

- Rust for the session managers, Python for tooling and diagnostics.
- You will debug against real captured traffic, not synthetic fixtures.
- Junior-friendly: we expect you to learn exchange protocols properly, not to arrive already knowing FIX inside out.""",
    "Founding Backend Engineer": """Lumen Order Flow is a small team building order-flow analytics for systematic traders. We are looking for someone who wants to be the second or third engineer: you will design services, build them, and own them in production.

- Python and Postgres to start, with Rust on the paths where it earns its place.
- You will talk to traders directly and turn what they say into schema.
- We are small enough that your first month is not onboarding.

If you want a role where owning something end to end is the job description rather than a bonus, this is it.""",
    "Software Engineer, Market Infrastructure": """Aurora Loop Systems is hiring an engineer for our market-infrastructure team. We build the plumbing that keeps prices, positions, and risk consistent across a multi-venue book.

- Rust for the core, Python for orchestration, Postgres for state.
- The team is four people; you will ship in week one.
- We are a young company and say so plainly in the interviews.""",
}

FILL_SHEET_FIELDS = [
    ("Full Name", "full_name", True, "identity.full_name", "exact"),
    ("Email", "email", True, "identity.email", "exact"),
    ("Phone", "phone", False, "identity.phone", "exact"),
    ("City", "city", True, "identity.location.city", "exact"),
    ("Country", "country", True, "identity.location.country", "exact"),
    ("LinkedIn", "linkedin", False, "identity.links.linkedin", "exact"),
    ("GitHub", "github", False, "identity.links.github", "exact"),
    ("Personal Website", "personal_website", False, "identity.links.personal_website", "exact"),
    ("Current Company", "current_company", True, "experience[EXPCUR].company", "exact"),
    ("Current Title", "current_title", True, "experience[EXPCUR].title", "exact"),
    ("Years of Experience", "years_experience", True, "experience[]", "derived"),
    ("Highest Degree", "highest_degree", True, "education[EDUCUR]", "exact"),
    ("School", "school", True, "education[EDUCUR].school", "exact"),
    ("Notice Period", "notice_period", False, "extras.notice_period", "exact"),
    ("How did you hear about us?", "referral", False, "extras.referrals.policy", "inferred"),
    ("Willing to relocate", "relocate", False, "other[X1]", "exact"),
    ("Do you require sponsorship?", "sponsorship", True, "work_authorization[SG]", "exact"),
    ("Gender", "eeo_gender", False, "extras.eeo.policy=decline", "declined"),
    ("Race / ethnicity", "eeo_race", False, "extras.eeo.policy=decline", "declined"),
    ("Veteran status", "eeo_veteran", False, "extras.eeo.policy=decline", "declined"),
]

# Deliberately absent from the master resume — this is what drives one demo
# packet to needs_input instead of ready.
MISSING_FIELD = ("Desired Salary", "desired_salary", False, "extras.salary.policy", "missing")

TAILOR_PLAN = {
    "summary": (
        "Backend and systems engineer with five years in market-data and execution "
        "infrastructure, currently moving Python hot paths to Rust; most recently cut "
        "p99 ingest latency from 340 ms to 45 ms by rebuilding the tick-normalisation "
        "worker in Rust, and designed the corporate-actions reference store now used "
        "by six downstream services."
    ),
    "skills_emphasis": ["rust", "c++", "python", "kafka", "postgresql",
                        "low-latency-systems", "market-data"],
    "experience_order": ["EXPCUR", "EXP1"],
    "keyword_alignment": {
        "matched": ["rust", "python", "low latency", "market data", "order book",
                    "profiling", "kafka", "postgresql"],
        "gaps": ["exchange protocol (FIX/ITCH) hands-on", "FPGA"],
    },
    "skills_drop": ["redis", "go", "kubernetes", "typescript"],
    "rephrased": [
        {"source_id": "EXPCUR-B1",
         "text": ("Rebuilt the tick-normalisation ingest as a Rust worker pool behind a "
                  "binary protocol, moving p99 latency from 340 ms to 45 ms for an 8x "
                  "throughput gain — the same shape of problem as your gateway layer.")},
    ],
    "projects": {"PRJ1": "kept — lock-free book-diff core is directly relevant"},
    "bullets": {
        "EXPCUR-B1": "emphasised: latency numbers and the profiling path",
        "EXPCUR-B2": "kept whole: schema ownership is the closest analogue",
        "EXPCUR-B3": "kept, shortened: on-call ownership framed as reliability work",
        "EXP1-B1": "emphasised: event-log replay maps onto order-state fan-out",
        "EXP1-B2": "kept whole: p95 query work on an indexed aggregate",
        "EXP1-B3": "kept: client-facing API ownership",
        "EXP1-B4": "dropped from one page, kept in two",
    },
}

RESUME_MD = """# Avery Nakamura
Singapore · avery@example.dev · +65 0000 0000
[linkedin.example/in/avery](https://linkedin.example/in/avery) · [github.example/avery](https://github.example/avery) · [example.dev](https://example.dev)

## Summary

Backend and systems engineer with five years in market-data and execution
infrastructure, currently moving Python hot paths to Rust. Most recently cut
p99 ingest latency from 340 ms to 45 ms by rebuilding the tick-normalisation
worker in Rust, and designed the corporate-actions reference store now used by
six downstream services.

## Experience

### Software Engineer, Market Data — Tessera Data Labs
_Singapore · Feb 2023 – present_

- Rebuilt the tick-normalisation ingest as a Rust worker pool behind a
  length-prefixed binary protocol, cutting **p99 latency from 340 ms to 45 ms**
  and lifting throughput **8x**.
- Designed the schema and dual-write backfill for the corporate-actions
  reference store now used by **6 downstream services**, migrated service by
  service with zero data incidents.
- Owned the on-call rotation for the ingest tier; audited every alert against a
  real incident and wrote the runbook, taking paging from **40 to 9 per week**.

### Backend Engineer — Pinewood Market Systems
_Singapore · Jul 2021 – Jan 2023_

- Built the order-survivorship replay service on an append-only event log;
  a 6-hour session replays in **under 30 seconds**.
- Cut **p95 query latency on alerts search from 1.9 s to 220 ms** by replacing
  per-alert queries with a single indexed aggregate.
- Shipped the client-facing alerts API (auth, pagination, rate limits) used by
  **3 institutional customers**.
- Mentored two junior engineers through their first production services.

## Project

### orderbook-replay — [example.dev/orderbook-replay](https://example.dev/orderbook-replay)
_2024 – present · solo_

- Wrote a lock-free book-diff core in Rust; **140 stars**, used by **3 teams**
  outside the company.
- Added a Python binding and a fixture-based test harness so non-Rust teams can
  contribute.

## Education

**BSc Computer Science**, National University of Singapore · 2017 – 2021 · GPA 4.6
Dean's List 2019, 2020. Thesis: lock-free ring buffer for order book fan-in,
1.8x throughput over a mutex baseline.

## Skills

**Languages** rust, c++, python, sql, go
**Systems** kafka, postgresql, redis, linux, perf-profiling
**Domains** market-data, order-book-reconstruction, event-sourcing, reference-data, exchange-connectivity
"""

COVER_LETTER = """Dear Hiring Team,

The part of your {title} posting that made me keep reading was the line about
owning the hop between the exchange gateways and the matching engine. That is
structurally the same problem as the one I spent the last two years on: a
Python ingest path at Tessera Data Labs that had hit a p99 of 340 ms and could
not get below it, because the bottleneck was the GIL rather than the network.
I profiled it, moved the hot path into a Rust worker pool behind a
length-prefixed binary protocol, and p99 went to 45 ms with 8x the throughput.
The measured numbers are on the attached resume; what I want to flag here is
that I care about the boring half — the budget per hop, the dashboard, the
profiling — as much as the rewrite.

I do not have exchange protocol work in my background. I have order book
reconstruction and event-log replay, and a lock-free diff core I wrote for it
(140 stars, three teams outside the company now run their tests against it),
and I would rather learn FIX properly at {company} than pretend I already have
it.

What draws me to {company} specifically is that the role is scoped to one
system with a measurable latency target rather than a spread of internal
tools. That is the work I want to keep doing.

I am in Singapore and do not need sponsorship. I can be available from the
first of next month.

Best regards,
Avery Nakamura
"""

MORNING_REPORT = """# jobscout morning report — {date}

**31 steps · $0.0412 spent**

## Final summary

Swept 24 queries across the banked rotation and self-generated angles. Three
findings worth a human's attention: **Vantage Point Markets** (dark-pool
A-tier entry, no ATS board, APAC expansion in the news), **Kestrel Financial
Systems** (careers page grew two engineering roles overnight), and **Aurora
Loop Systems** (an engineering "why we are hiring" post, no careers page link
anywhere — worth a cold email).

The rest of the sweep was noise. I did not add any of the data vendors; they
are all ATS-saturated, which is the exact opposite of the thesis.

## DB changes

- companies: 21 -> 24 (+3)
- signals: 38 -> 44 (+6)
- candidates: 9 -> 10 (+1)

## Companies found

| Company | Domain | Tier | Evidence |
|---|---|---|---|
| Vantage Point Markets | vantagepoint.example | candidate | news: "expands Singapore trading desk" |
| Kestrel Financial Systems | kestrelfs.example | candidate | careers_change: +2 engineering roles |
| Aurora Loop Systems | auroraloop.example | candidate | blog: "why we are hiring" |
| Cobalt Ridge Data | cobaltridge.example | candidate | hn: order book replay tooling (84 pts) |

## Suggested actions

1. **Promote Kestrel to B** — two back-to-back engineering roles on a
   dark-pool firm is the strongest single signal in this report.
2. **Cold email Aurora Loop** — no careers page means no ATS board, so nothing
   in the pipeline will ever find them.
3. **Watch Vantage Point** — it is already an A-tier entry with an empty ATS
   map. The news gives it a reason to hire now.

## Tool log

### step 1 · `get_context`
- args: `{{}}`
- result: watchlist: A=6, B=5, C=3, candidate=10 · profile v2026-09-30.2

### step 2 · `web_search`
- args: `{{"query": "Vantage Point Markets Singapore hiring 2026"}}`
- result: 8 results; newswire item confirms APAC desk expansion

### step 3 · `fetch`
- args: `{{"url": "https://kestrelfs.example/careers"}}`
- result: 2 new engineering roles vs yesterday's snapshot

### step 4 · `add_company`
- args: `{{"name": "Aurora Loop Systems", "domain": "auroraloop.example"}}`
- result: added as candidate; no ATS board detected

---
state: companies=24 (was 21), signals=44 (was 38), candidates=10 (was 9)
"""

DAILY_REPORT = """# jobscout daily digest — {date}

**Sources: 14 ok · 0 errors · {seen} postings seen · {new} new**

## New postings by source

| Source | Seen | New |
|---|---|---|
| ats:greenhouse | 84 | 12 |
| ats:lever | 51 | 9 |
| ats:ashby | 22 | 5 |
| careers crawl | 14 | 3 |
| mycareersfuture | 11 | 2 |
| rss | 5 | 0 |

## Rule gate

{new} new postings, {passed} passed the deterministic filter, {excluded} excluded
(4 senior/lead/principal titles, 2 outside the location list, 1 clearance
required).

## Scored

{passed} postings scored on the cheap tier for **$0.0031** (cached — 0 re-scores
of unchanged content). Top three:

1. **Low-Latency Systems Engineer** — Cobalt Exchange Systems · 89
2. **Quant Developer — Execution** — Meridian Quant Partners · 86
3. **Software Engineer** — Vantage Point Markets · 86

## Signals

6 new: 3 careers-page changes, 2 news, 1 funding. Kestrel Financial Systems'
careers page grew two roles — the agent will pick that up tomorrow morning.

## Candidates surfaced

3 new from MyCareersFuture: Aurora Loop Systems, Cobalt Ridge Data,
Driftwood Markets. None have ATS boards, so they are dark-pool by default.
"""

RESEARCH_NOTE = """# Kestrel Financial Systems — careers page diff

## 2026-09-30

```diff
+ /careers/backend-engineer-post-trade
+ /careers/data-platform-engineer
```

Two engineering roles appeared overnight on a firm with **no ATS board**.
That is the dark-pool signature: hiring that never touches a job board.

**Action:** promote to B, and check whether the careers page has a contact
inbox — the draft cold email lives on the Applications page.
"""


# ── config + resume inputs ───────────────────────────────────────────────────

def write_settings(home: Path) -> None:
    (home / "config" / "settings.yaml").write_text(yaml.safe_dump({
        "discovery": {
            "mode": "hybrid",
            "agent": {
                "schedule": "weekdays", "max_steps": 60, "max_cost_usd": 0.50,
                "model": "agent", "run_on_signal": True,
            },
            "pipeline": {
                "ats_boards": True, "careers_crawl": True, "cse_queries_per_day": 10,
                "rss": True, "job_sites": True,
            },
        },
        "dashboard": {"host": "127.0.0.1", "port": 8801},
        "sidecar": {"enabled": True, "path": "../JobPilot", "command": "npm run dev"},
        "llm": {"provider": "openai-compatible",
                "base_url_env": "JOBSCOUT_LLM_BASE_URL",
                "api_key_env": "JOBSCOUT_LLM_API_KEY"},
        "run": {"daily_hour": 6, "daily_minute": 30, "agent_hour": 7, "agent_minute": 0,
                "skip_if_run_within_hours": 20},
        "search": {"provider": "auto", "llm_model": "perplexity/sonar"},
        "email": {"enabled": False, "poll_minutes": 15},
    }, sort_keys=False), encoding="utf-8")


def write_profile(home: Path) -> None:
    (home / "config" / "profile.yaml").write_text(yaml.safe_dump({
        "profile_version": "2026-09-30.2",
        "target": {
            "roles": ["quant developer", "low-latency engineer", "backend engineer"],
            "weighting": {"quant": 0.7, "general-backend": 0.3},
            "seniorities": ["junior"],
            "locations": ["Singapore", "Hong Kong", "Amsterdam",
                          "San Francisco", "New York"],
            "primary_locations": ["Singapore"],
            "remote": {"allowed": True, "preference": "hybrid"},
            "domains": ["market-data", "execution", "exchange-infrastructure",
                        "low-latency-systems"],
            "stack": ["rust", "c++", "python", "kafka", "postgresql"],
        },
        "dealbreakers": ["clearance_required", "on_call_heavy"],
        "company_tiers": {"A": [], "B": [], "C": []},
        "scoring_rubric": {"fit": 0.6, "company_quality": 0.25, "opportunity": 0.15},
        "comp_floor": {"currency": "SGD", "amount": None},
    }, sort_keys=False), encoding="utf-8")


def write_watchlist(home: Path) -> None:
    # the promote table's Note column IS the evidence, so each candidate's
    # note is the signal that justified it
    evidence = {company: note for company, _kind, note in SIGNAL_NOTES}
    out: dict[str, list] = {"A": [], "B": [], "C": [], "candidates": []}
    for tier, rows in TIERS.items():
        for name, domain, ats, note in rows:
            out[tier].append({"name": name, "domain": domain, "ats": ats, "note": note})
    for name, domain, via in CANDIDATES:
        out["candidates"].append({
            "name": name, "domain": domain, "ats": {},
            "note": evidence.get(name, f"discovered via {via}"),
            "found_via": via,
        })
    (home / "config" / "watchlist.yaml").write_text(
        yaml.safe_dump(out, sort_keys=False, width=100), encoding="utf-8")


def write_resume(home: Path) -> None:
    c = CANDIDATE
    resume = {
        "version": 4,
        "identity": {
            "full_name": c["name"], "preferred_name": "Avery",
            "email": c["email"], "phone": c["phone"],
            "location": {"city": c["city"], "region": None, "country": c["country"],
                         "timezone": "Asia/Singapore"},
            "links": {"personal_website": c["website"], "linkedin": c["linkedin"],
                      "github": c["github"],
                      "extra": [{"id": "L1", "label": "blog", "url": c["website"]}]},
        },
        "work_authorization": [{"country": "SG", "status": "citizen",
                                "sponsorship_needed": False,
                                "note": "Singapore citizen; no sponsorship required."}],
        "education": [{
            "id": "EDUCUR", "school": c["school"], "degree": "BSc",
            "field": "Computer Science",
            "dates": {"start": "2017-08", "end": "2021-05"}, "gpa": 4.6,
            "honors": ["Dean's List 2019, 2020"],
            "highlights": ["Thesis: lock-free ring buffer for order book fan-in, "
                           "1.8x throughput over a mutex baseline"],
        }],
        "experience": [
            {
                "id": "EXPCUR", "company": c["current_company"], "domain": "tessera.example",
                "context": "Builds market-data pipelines and reference-data services for Asian funds",
                "title": c["current_title"], "titles": [],
                "dates": {"start": "2023-02", "end": "present"},
                "location": "Singapore", "employment": "full-time",
                "tech": ["rust", "python", "kafka", "postgresql"],
                "bullets": [
                    {"id": "EXPCUR-B1",
                     "text": "Rewrote the tick-normalisation ingest from a Python/threading pipeline to a Rust worker pool, cutting p99 ingest latency from 340 ms to 45 ms",
                     "metrics": ["p99 latency 340 ms -> 45 ms", "8x throughput on the normaliser"],
                     "tech": ["rust", "kafka"], "tags": ["latency", "market-data", "systems"]},
                    {"id": "EXPCUR-B2",
                     "text": "Designed the schema and backfill strategy for the corporate-actions reference store now used by 6 downstream services",
                     "metrics": ["6 downstream services migrated with zero data incidents"],
                     "tech": ["postgresql", "python"],
                     "tags": ["data-modeling", "ownership", "reference-data"]},
                    {"id": "EXPCUR-B3",
                     "text": "Owned the on-call rotation for the ingest tier; wrote the runbook and cut the paging alert count from 40 to 9 per week",
                     "metrics": ["alert noise 40 -> 9 per week"], "tech": [],
                     "tags": ["operations", "reliability"]},
                ],
            },
            {
                "id": "EXP1", "company": "Pinewood Market Systems",
                "domain": "pinewoodms.example",
                "context": "Surveillance and order-monitoring tooling for prime brokers",
                "title": "Backend Engineer",
                "titles": [{"title": "Software Engineer", "from": "2021-07", "to": "2022-11"}],
                "dates": {"start": "2021-07", "end": "2023-01"},
                "location": "Singapore", "employment": "full-time",
                "tech": ["python", "postgresql", "kafka"],
                "bullets": [
                    {"id": "EXP1-B1",
                     "text": "Built the order-survivorship replay service that lets clients reconstruct a session's order state from an append-only event log",
                     "metrics": ["replay of a 6-hour session in under 30 seconds"],
                     "tech": ["python", "kafka"], "tags": ["backend", "event-sourcing", "api-design"]},
                    {"id": "EXP1-B2",
                     "text": "Cut p95 query latency on the alerts search from 1.9 s to 220 ms by moving from per-alert queries to a single indexed aggregate",
                     "metrics": ["p95 1.9 s -> 220 ms"], "tech": ["postgresql"],
                     "tags": ["performance", "database"]},
                    {"id": "EXP1-B3",
                     "text": "Shipped the client-facing alerts API (auth, pagination, rate limits) used by 3 institutional customers",
                     "metrics": ["3 institutional customers on self-serve"], "tech": ["python"],
                     "tags": ["api-design", "product"]},
                    {"id": "EXP1-B4",
                     "text": "Mentored two junior engineers through their first production services",
                     "metrics": [], "tech": [], "tags": ["mentoring"]},
                ],
            },
        ],
        "projects": [{
            "id": "PRJ1", "name": "orderbook-replay",
            "url": f"{CANDIDATE['website']}/orderbook-replay",
            "dates": {"start": "2024-03", "end": "present"}, "role": "solo",
            "context": "A small open-source tool for replaying captured order books and diffing them against a model",
            "tech": ["rust", "python"],
            "bullets": [
                {"id": "PRJ1-B1",
                 "text": "Wrote a lock-free book-diff core in Rust; 140 stars and used by three teams outside the company",
                 "metrics": ["140 stars", "3 external teams using it"], "tech": ["rust"],
                 "tags": ["open-source", "performance"]},
                {"id": "PRJ1-B2",
                 "text": "Added a Python binding and a fixture-based test harness so non-Rust teams can contribute",
                 "metrics": [], "tech": ["python"],
                 "tags": ["open-source", "developer-experience"]},
            ],
        }],
        "skills": {
            "core": [
                {"area": "languages", "items": ["rust", "c++", "python", "sql", "go"]},
                {"area": "systems",
                 "items": ["kafka", "postgresql", "redis", "linux", "perf-profiling"]},
                {"area": "domains",
                 "items": ["market-data", "order-book-reconstruction", "event-sourcing",
                           "reference-data", "exchange-connectivity"]},
            ],
            "certifications": [], "publications": [],
        },
        "extras": {
            "salary": {"policy": "defer to conversation", "floor": None, "currency": "SGD"},
            "notice_period": "1 month", "availability": "Immediately",
            "languages_spoken": ["English", "Japanese"],
            "interests": ["open-source systems", "long-distance running", "coffee",
                          "board games"],
            "referrals": {"policy": "company careers page"},
            "eeo": {"policy": "decline"},
        },
        "other": [
            {"id": "X1", "label": "willing to relocate",
             "value": "Hong Kong, Amsterdam; Singapore preferred"},
            {"id": "X2", "label": "portfolio",
             "value": f"{CANDIDATE['website']}/orderbook-replay"},
        ],
    }
    (home / "master_resume" / "resume.yaml").write_text(
        "# DEMO COPY — every fact here is invented for the README assets.\n"
        "# The real file lives in the repo; this one lets the documented packet\n"
        "# flow be exercised end to end without touching private data.\n"
        + yaml.safe_dump(resume, sort_keys=False, width=100), encoding="utf-8")

    (home / "master_resume" / "narrative.md").write_text(
        "# narrative.md — STAR stories keyed to bullet IDs (PLAN §5.2)\n\n"
        "DEMO COPY — invented stories, safe to publish.\n\n"
        "## STORY-S1 (refs: EXPCUR-B1)\n\n"
        "**Situation** — the ingest was the busiest path and the p99 was dominated by\n"
        "the GIL, not the network.\n"
        "**Task** — keep the normaliser up with three exchange feeds at peak.\n"
        "**Action** — profiled it, moved the hot path into a Rust worker pool behind a\n"
        "length-prefixed binary protocol, keeping Python at the edges.\n"
        "**Result** — p99 340 ms -> 45 ms, throughput up 8x (anchors in EXPCUR-B1).\n",
        encoding="utf-8")

    (home / "master_resume" / "extras.md").write_text(
        "# extras.md — additional information (long-form, PLAN §5.2)\n\n"
        "DEMO COPY — invented context, safe to publish.\n\n"
        "- **Compensation.** I have not anchored on a number publicly. The structured\n"
        "  policy lives in `resume.yaml` `extras.salary`; this paragraph is the *why*.\n"
        "- **Relocation.** Singapore citizen, no sponsorship needed. Happy to relocate to\n"
        "  Hong Kong or Amsterdam, but a hybrid Singapore setup is my preference.\n"
        "- **What I want next.** A small team that owns a system end to end, in market\n"
        "  data or execution infrastructure.\n"
        "- **Tone constraints.** Do not open with \"I am writing to apply\". Do not say\n"
        "  \"passionate\". Lead with the closest measured result from the master resume.\n",
        encoding="utf-8")


# ── database + packets ───────────────────────────────────────────────────────

def build_description(company: str, title: str, location: str, rng: random.Random) -> str:
    """Location always comes from the posting's own row, so the details panel
    and the body can never disagree."""
    if title in HANDWRITTEN_DESCRIPTIONS:
        return HANDWRITTEN_DESCRIPTIONS[title]
    return (
        f"{company} is hiring a {title.lower()}. "
        f"You will work on {rng.choice(['the core trading stack', 'market-data infrastructure', 'execution and routing', 'our post-trade services', 'the data platform', 'exchange connectivity'])}.\n\n"
        f"- Full-time, {location}.\n"
        f"- Stack: {rng.choice(['Rust, C++ and Python', 'Python, Postgres and Kafka', 'Rust and Python on Linux', 'Go, Postgres and Kubernetes'])}.\n"
        "- Small team, high ownership, direct access to the people who built the system.\n\n"
        "We review every application ourselves and reply either way."
    )


def fill_sheet(company: str, title: str, with_missing: bool) -> dict:
    fields = list(FILL_SHEET_FIELDS) + ([MISSING_FIELD] if with_missing else [])
    values = {
        "identity.full_name": CANDIDATE["name"],
        "identity.email": CANDIDATE["email"],
        "identity.phone": CANDIDATE["phone"],
        "identity.location.city": CANDIDATE["city"],
        "identity.location.country": CANDIDATE["country"],
        "identity.links.linkedin": CANDIDATE["linkedin"],
        "identity.links.github": CANDIDATE["github"],
        "identity.links.personal_website": CANDIDATE["website"],
        "experience[EXPCUR].company": CANDIDATE["current_company"],
        "experience[EXPCUR].title": CANDIDATE["current_title"],
        "experience[]": "5",
        "education[EDUCUR]": CANDIDATE["degree"],
        "education[EDUCUR].school": CANDIDATE["school"],
        "extras.notice_period": "1 month",
        "extras.referrals.policy": "company careers page",
        "other[X1]": "Hong Kong, Amsterdam; Singapore preferred",
        "work_authorization[SG]": "No",
    }
    out = []
    for (label, key, required, source, conf) in fields:
        value = values.get(source, "")
        out.append({
            "label": label, "canonical_key": key, "required": required,
            "value": value, "value_source": source, "confidence": conf,
            "note": "" if conf != "missing" else "no value in master resume — needs your input",
        })
    return {"posting_title": title, "company": company, "fields": out}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--home", required=True,
                    help="target JOBSCOUT_HOME (created; never the live repo)")
    ap.add_argument("--seed", type=int, default=20261002)
    ap.add_argument("--fresh", action="store_true", help="wipe the target first")
    args = ap.parse_args()

    console = Console()
    home = Path(args.home).expanduser().resolve()
    if args.fresh and home.exists():
        shutil.rmtree(home)
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "master_resume").mkdir(parents=True, exist_ok=True)

    repo = Path(__file__).resolve().parent.parent
    # models.yaml is pricing config, not personal data — reuse the repo's copy
    shutil.copyfile(repo / "config" / "models.yaml", home / "config" / "models.yaml")
    write_settings(home)
    write_profile(home)
    write_watchlist(home)
    write_resume(home)

    # everything below resolves the runtime from JOBSCOUT_HOME
    os.environ["JOBSCOUT_HOME"] = str(home)
    for mod in [m for m in list(sys.modules) if m.startswith("jobscout")]:
        del sys.modules[mod]
    from jobscout.core import db
    from jobscout.core import paths as core_paths
    from jobscout.core.resume import load_master_resume
    from jobscout.packets import claim_check
    from jobscout.scoring.llm_bulk import compute_final
    from jobscout.sources.postings.base import RawPosting

    db.init_db()
    rng = random.Random(args.seed)
    now = datetime.now(UTC)

    def iso(days_ago: float, hour: int = 6, minute: int = 30) -> str:
        return (now - timedelta(days=days_ago)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    def day(days_ago: float) -> str:
        return (now - timedelta(days=days_ago)).strftime("%Y-%m-%d")

    conn = db.connect()
    try:
        # companies
        for tier, rows in TIERS.items():
            for name, domain, ats, note in rows:
                db.upsert_company(conn, name=name, domain=domain, tier=tier,
                                  ats_tokens=ats or None,
                                  career_url=f"https://{domain}/careers", notes=note)
        for name, domain, _via in CANDIDATES:
            db.upsert_company(conn, name=name, domain=domain, tier="candidate")

        # postings — scores via the pipeline's own compute_final
        for (company, title, location, seniority, source, fit, opp,
             days_ago, status) in POSTINGS:
            slug = db.slugify(company)
            url = (f"https://{db.slugify(company)}.example/jobs/"
                   f"{hashlib.sha1(title.encode()).hexdigest()[:6]}")
            description = build_description(company, title, location, rng)
            db.upsert_postings(conn, [RawPosting(
                source=source, company=company, company_slug=slug, title=title,
                url=url, location=location,
                remote="remote" in location.lower() or None,
                description=description, posted_at=iso(days_ago, 9, 0),
                job_type="full-time", seniority=seniority,
            )])
            pid = conn.execute("SELECT id FROM postings WHERE url_hash = ?",
                               (db.sha256(url),)).fetchone()["id"]
            # backdate: the public helper always stamps "now"
            conn.execute(
                "UPDATE postings SET first_seen = ?, last_seen = ?, posted_at = ?"
                " WHERE id = ?",
                (iso(days_ago + 0.2), iso(days_ago - 0.4), iso(days_ago, 9, 0), pid))

            rule_passed = fit is not None
            db.set_rule_pass(conn, pid, rule_passed)
            if rule_passed:
                parsed = {
                    "fit": round(fit * 100),
                    "stack": [s for s in ("rust", "c++", "python", "kafka", "postgresql")
                              if s in description.lower()],
                    "seniority": seniority or "unspecified",
                    "flags": [], "red_flags": [],
                    "rationale": "systems role in market infrastructure; stack overlaps the profile",
                    "competition": "low" if opp >= 0.80 else ("medium" if opp >= 0.70 else "high"),
                    "id": pid,
                }
                crow = conn.execute(
                    "SELECT tier, non_ats, ats_tokens FROM companies WHERE id = ?",
                    (slug,)).fetchone()
                s = compute_final(parsed, crow["tier"], crow["non_ats"],
                                  len(json.loads(crow["ats_tokens"] or "{}")))
                db.update_posting_scores(
                    conn, pid, fit=s["fit"], company_quality=s["company_quality"],
                    opportunity=s["opportunity"], final=s["final"],
                    llm_json=json.dumps(parsed),
                    cache_key=db.sha256(f"{db.sha256(title)}|2026-09-30.2"))
            if status != "new":
                db.set_posting_status(conn, pid, db.canon_status(status))
            conn.commit()

        # signals
        for i, (company, kind, note) in enumerate(SIGNAL_NOTES):
            conn.execute(
                "INSERT OR IGNORE INTO signals (id, company_id, kind, payload, note, seen_at)"
                " VALUES (?,?,?,?,?,?)",
                (db.sha256(f"{kind}|{db.sha256(note)}"), db.slugify(company), kind,
                 json.dumps({"note": note}), note, iso(0.2 + i * 0.11, 7, 5)))
        conn.commit()

        # run history — the Ops + Discovery pages both read these stat keys
        for i in range(12):
            d = 11 - i
            rid = db.record_run(conn, "daily")
            conn.execute("UPDATE runs SET started = ?, finished = ?, ok = 1 WHERE id = ?",
                         (iso(d), iso(d, 6, 42), rid))
            base = {"ats:greenhouse": 84, "ats:lever": 51, "ats:ashby": 22,
                    "careers crawl": 14, "mycareersfuture": 11, "rss": 5}
            if i == 0:
                srcs = {k: v + rng.randint(2, 9) for k, v in base.items()}
            else:
                srcs = {k: max(0, v + rng.randint(-14, 14)) for k, v in base.items()}
            conn.execute("UPDATE runs SET stats = ? WHERE id = ?", (json.dumps({
                "companies": rng.randint(0, 4), "postings_seen": sum(srcs.values()),
                "new": rng.randint(14, 37), "scored": rng.randint(10, 30),
                "rule_pass": rng.randint(8, 28), "signals": rng.randint(0, 6),
                "errors": 0, "sources": srcs,
            }), rid))
        for i in range(6):
            rid = db.record_run(conn, "agent")
            # the agent starts an hour after the sweep — which is exactly why
            # the Ops source-health card must filter on kind, not take runs[0]
            conn.execute(
                "UPDATE runs SET started = ?, finished = ?, ok = 1, cost_usd = ?"
                " WHERE id = ?",
                (iso(10 - i * 2, 7, 0), iso(10 - i * 2, 7, 19),
                 round(rng.uniform(0.02, 0.06), 4), rid))
            conn.execute("UPDATE runs SET stats = ? WHERE id = ?", (json.dumps({
                "companies": rng.randint(0, 3), "postings_seen": 0,
                "steps": rng.randint(22, 38), "queries": rng.randint(18, 26),
                "signals": rng.randint(3, 7),
            }), rid))
        conn.commit()

        # 30 days of LLM spend so the cost cards are not empty
        for d in range(30):
            for _ in range(rng.randint(6, 16)):
                conn.execute(
                    "INSERT INTO llm_calls (tier, model, cache_key, prompt_tokens,"
                    " completion_tokens, cost_usd, created_at) VALUES (?,?,?,?,?,?,?)",
                    ("bulk", "cheap-scoring", None, rng.randint(700, 1100),
                     rng.randint(90, 180), round(rng.uniform(0.00008, 0.00031), 6),
                     iso(d, 6, 36)))
            for _ in range(rng.randint(0, 2)):
                conn.execute(
                    "INSERT INTO llm_calls (tier, model, cache_key, prompt_tokens,"
                    " completion_tokens, cost_usd, created_at) VALUES (?,?,?,?,?,?,?)",
                    ("agent", "agent-reasoning", None, rng.randint(6000, 14000),
                     rng.randint(1200, 3000), round(rng.uniform(0.008, 0.032), 6),
                     iso(d, 7, 8)))
            if d < 9 and rng.random() < 0.5:
                conn.execute(
                    "INSERT INTO llm_calls (tier, model, cache_key, prompt_tokens,"
                    " completion_tokens, cost_usd, created_at) VALUES (?,?,?,?,?,?,?)",
                    ("quality", "quality-tailor", None, 8200, 2400,
                     round(rng.uniform(0.041, 0.118), 4), iso(d, 8, 20)))
        conn.commit()

        # cache + scoring + state pointers
        for row in conn.execute(
                "SELECT content_hash FROM postings WHERE llm_cache_key IS NOT NULL"
        ).fetchall():
            conn.execute(
                "INSERT OR IGNORE INTO llm_cache (cache_key, response, model)"
                " VALUES (?,?,?)",
                (db.sha256(f"{row['content_hash']}|2026-09-30.2"),
                 json.dumps({"fit": 80}), "cheap-scoring"))
        conn.execute(
            "INSERT OR REPLACE INTO scoring_state (id, scored_profile_hash,"
            " pending_profile_hash, pending_remaining, pending_limit, full_passes,"
            " last_run_id, updated_at) VALUES (1, ?, NULL, 0, NULL, 7, 12, datetime('now'))",
            (db.sha256("2026-09-30.2"),))
        for k, v in (("last_daily_run", day(0)), ("last_agent_run", day(1)),
                     ("hn_last_story", "41883477"), ("cse_query_pointer", "7/10")):
            db.set_state(conn, k, v)
        conn.commit()

        # packets — status DERIVED from the real claim-check gate so the board
        # can never disagree with the table it renders
        resume = load_master_resume()
        made = []
        for (company, title, want_missing, dago, cost) in [
            ("Cobalt Exchange Systems", "Low-Latency Systems Engineer", False, 0.4, 0.0934),
            ("Meridian Quant Partners", "Quant Developer — Execution", True, 1.2, 0.0711),
            ("Halcyon Markets", "Backend Engineer, Market Data", False, 0.8, 0.0588),
        ]:
            row = conn.execute(
                "SELECT p.id, p.url FROM postings p JOIN companies c ON p.company_id = c.id"
                " WHERE c.name = ? AND p.title = ?", (company, title)).fetchone()
            if row is None:
                continue
            packet_id = f"pk-{db.sha256(row['id'])[:12]}"
            out_dir = f"{db.slugify(company).replace('-', '')}-{day(dago)}"
            pdir = core_paths.applications_dir() / out_dir
            pdir.mkdir(parents=True, exist_ok=True)

            letter = COVER_LETTER.format(company=company, title=title)
            claims = claim_check.check_packet(
                TAILOR_PLAN, letter, resume, db.get_posting(conn, row["id"]))
            passed = claim_check.gate(claims)

            reasons: list[str] = []
            if want_missing:
                reasons.append("master resume missing 1 field(s): Desired Salary")
            if not passed:
                reasons.append(
                    f"claim-check: {claim_check.unsupported_count(claims)} unsupported"
                    " claim(s) must be resolved before this packet can go ready")
            status = "ready" if not reasons else "needs_input"

            (pdir / "fill_sheet.yaml").write_text(
                yaml.safe_dump(fill_sheet(company, title, want_missing),
                               sort_keys=False, width=100), encoding="utf-8")
            (pdir / "claim_check.yaml").write_text(
                yaml.safe_dump(claims, sort_keys=False, width=100), encoding="utf-8")
            (pdir / "tailor.yaml").write_text(
                yaml.safe_dump(TAILOR_PLAN, sort_keys=False, width=100), encoding="utf-8")
            (pdir / "resume.md").write_text(RESUME_MD, encoding="utf-8")
            (pdir / "cover_letter.md").write_text(letter, encoding="utf-8")
            (pdir / "packet.yaml").write_text(yaml.safe_dump({
                "packet_id": packet_id, "posting_id": row["id"], "posting_title": title,
                "company": company, "posting_url": row["url"], "status": status,
                "reasons": reasons or ["all required fields resolved from the master resume"],
                "model": "quality-tailor", "cost_usd": cost,
                "generated": iso(dago, 8, 2),
                "files": ["claim_check.yaml", "cover_letter.md", "fill_sheet.yaml",
                          "resume.md", "tailor.yaml"],
            }, sort_keys=False, width=100), encoding="utf-8")

            db.upsert_packet(conn, packet_id=packet_id, posting_id=row["id"],
                             status=status, dir_path=f"applications/{out_dir}",
                             model="quality-tailor", cost_usd=cost)
            conn.execute("UPDATE packets SET created_at = ?, updated_at = ? WHERE id = ?",
                         (iso(dago, 8, 2), iso(dago, 8, 2), packet_id))
            db.set_posting_status(conn, row["id"], f"packet:{status}")
            made.append((packet_id, company, dago))
        conn.commit()

        # one application part-way through the funnel
        if made:
            pid, company, dago = max(made, key=lambda m: m[2])
            for kind, off, t, notes in [
                ("applied", 12, "Application submitted",
                 "Submitted via the packet; resume + cover letter attached."),
                ("oa", 10, "Online assessment",
                 "90-minute take-home: fix the ordering bug in the provided handler."),
                ("phone_screen", 8, "Recruiter call",
                 "30 min with the trading-platforms lead."),
                ("onsite", 5, "Onsite loop",
                 "System design on a venue failover, then pairing on a Rust log consumer."),
            ]:
                db.record_app_event(conn, packet_id=pid, kind=kind,
                                    event_date=day(dago + off), title=t, notes=notes)
            db.record_apply_run(conn, packet_id=pid, mode="assisted", status="submitted",
                                detail="form filled to the final submit; owner pressed submit")
            db.record_email_event(
                conn, company_id=db.slugify(company), packet_id=pid,
                from_addr=f"talent@{db.slugify(company).replace('-', '')}.example",
                subject="Re: application — next steps", sent_at=iso(dago + 7, 9, 14),
                message_id="<demo-1>", classification="interview_invite",
                action="state: interviewing", detail=json.dumps({"confidence": 0.94}))
    finally:
        conn.close()

    # reports + research on disk
    for d in range(0, 10, 2):
        (core_paths.morning_reports_dir() / f"{day(d)}.md").write_text(
            MORNING_REPORT.format(date=day(d)), encoding="utf-8")
    for d in range(12):
        (core_paths.digest_dir() / f"{day(d)}.md").write_text(
            DAILY_REPORT.format(date=day(d), seen=187, new=31, passed=24, excluded=7),
            encoding="utf-8")
    (core_paths.research_dir() / "kestrel-careers-diff.md").write_text(
        RESEARCH_NOTE, encoding="utf-8")

    conn = db.connect()
    try:
        counts = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                  for t in ("companies", "postings", "signals", "packets", "runs")}
    finally:
        conn.close()

    console.print(f"[green]✓[/] demo runtime seeded: [bold]{home}[/]")
    console.print("  " + "  ".join(f"{k}={v}" for k, v in counts.items()))
    console.print(f"  [dim]run:[/] JOBSCOUT_HOME={home} .venv/bin/jobscout serve --port 8801")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
