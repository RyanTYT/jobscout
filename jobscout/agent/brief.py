"""Morning Brief builder (PLAN §1.2) — the agent's instructions, rendered
from live state. The deterministic collectors have already run; the agent's
job is query generation + judgment on top of them."""

from __future__ import annotations

import sqlite3


def build_brief(conn: sqlite3.Connection, profile, settings) -> str:
    agent_cfg = settings.discovery.agent
    target = profile.target
    levels = "/".join(target.seniorities) if target.seniorities else "any level"
    primary = ", ".join(target.primary_locations)
    others = [loc for loc in target.locations if loc not in target.primary_locations]
    loc_line = (
        f"locations={primary} (preferred)"
        + (f"; also acceptable: {', '.join(others)}" if others else "")
        if target.locations
        else "locations=any"
    )
    return f"""MORNING BRIEF — {__date_hint__()}

You are the discovery layer for a {levels} engineer hunting for
{", ".join(target.roles) or "engineering"} roles, with a focus on firms
WITHOUT strong job-board presence (the "dark pool" — fewer applicants,
better odds).

Candidate target (distilled): roles={", ".join(target.roles)};
stack={", ".join(target.stack)}; domains={", ".join(target.domains)};
seniorities={", ".join(target.seniorities)}; {loc_line}.
Prefer {primary or "no specific location"}-based roles when weighing
promising companies; treat the other acceptable locations as solid
runners-up, not equal-priority. Remote roles are acceptable (preference:
{target.remote.preference}).

The deterministic collectors (ATS APIs, careers crawls, HN thread, RSS) have
ALREADY run today. Your value-add is judgment and open-ended search.

Steps:
1. Call get_context() first. Note the unresolved funding mentions — resolving
   one of those (find its domain via web_search) is high value.
2. Sweep: 5-15 web searches, mixing TWO kinds:
   (a) PROFILE-DRIVEN — companies that fit the candidate profile itself,
       not just today's signals. Build queries directly from the target
       above: roles x stack x domains x locations. Examples (adapt, don't
       copy): '"<stack keyword>" "<role>" <primary location> hiring',
       'companies building <domain> in <primary location>',
       '"<role>" <acceptable location> startup hiring'. The goal: find
       EMPLOYERS whose business looks like this profile, then add_company
       them.
   (b) SIGNAL-DRIVEN — funding news for relevant sectors, "we are hiring"
       posts, stealth startups in target domains, exchanges/market-infra
       hiring.
   Do not repeat what's already known.
3. For each promising company found: add_company with a REAL domain (never
   invent) and a one-line reason citing your source.
4. Record observations on known companies with add_signal (one-line, sourced).
5. Pick the 1-3 most promising dark-pool leads (companies with hiring
   signals but no postings). Fetch their careers pages / news, then
   write_note a short research note (with sources and dates).
6. End with a final summary: what you added, what you'd promote, what you
   discarded and why.

Rules:
- Every add needs a one-line rationale. No invented domains or facts.
- Promotion to tiers A/B/C stays with the owner — propose, don't promote.
- Frugal with steps: caps are {agent_cfg.max_steps} steps and
  ${agent_cfg.max_cost_usd:.2f} for this run.
- If web_search reports no provider configured, rely on fetch() with known
  URLs, resolve unresolved mentions, and say so in the summary."""


def __date_hint__() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
