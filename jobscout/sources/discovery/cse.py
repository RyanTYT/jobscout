"""Google Programmable Search (CSE) discovery (PLAN §6.1, §10 P4).

Free tier: 100 queries/day. A date-rotated query bank (no state needed):
the day's ordinal picks a starting offset into the bank, wrapping. Each
result domain that isn't an aggregator and isn't already watched becomes
a live-probed watchlist candidate.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from urllib.parse import urlparse

import httpx

from jobscout.core import db
from jobscout.sources.discovery.blocklist import is_blocked
from jobscout.sources.postings.base import soft_get

_API = "https://www.googleapis.com/customsearch/v1"

QUERY_BANK = (
    '"quantitative developer" hiring',
    '"quant developer" careers',
    '"trading systems engineer" hiring',
    '"market data engineer" careers',
    '"low latency" C++ engineer hiring',
    '"execution engineer" careers',
    'rust engineer "we are hiring"',
    '"exchange engineer" hiring',
    '"core developer" "proprietary trading"',
    '"derivatives engineer" hiring',
    '"infrastructure engineer" trading firm',
    '"backtesting" engineer hiring',
    '"order management system" engineer careers',
    '"matching engine" developer hiring',
    '"fpga engineer" trading careers',
    '"site reliability" engineer finance',
    '"platform engineer" fintech hiring',
    '"backend engineer" market infrastructure',
)

MAX_CSE_CANDIDATES = 5


def _bank(profile) -> list[str]:
    bank = list(QUERY_BANK)
    for role in profile.target.roles:
        q = f'"{role}" hiring'
        if q not in bank:
            bank.append(q)
    for kw in profile.target.domains:
        q = f'"{kw}" engineer hiring'
        if q not in bank:
            bank.append(q)
    return bank


def queries_for_today(profile, n: int, today: date | None = None) -> list[str]:
    """Deterministic rotation: no stored state, stable per day."""
    today = today or datetime.now(UTC).date()
    bank = _bank(profile)
    start = today.toordinal() % len(bank)
    return [bank[(start + i) % len(bank)] for i in range(min(n, len(bank)))]


def sweep(
    client: httpx.Client,
    conn,
    wl,
    profile,
    env: dict,
    per_day: int,
    add_candidate,
) -> dict:
    key = (env.get("JOBSCOUT_CSE_API_KEY") or "").strip()
    cx = (env.get("JOBSCOUT_CSE_CX") or "").strip()
    if not key or not cx:
        return {"skipped": "no CSE key/cx (set JOBSCOUT_CSE_* in .env)"}

    known_domains = {
        e.domain.lower() for tier in ("A", "B", "C", "candidates")
        for e in getattr(wl, tier) if e.domain
    }
    known_by_domain: dict[str, tuple[str, str]] = {}
    for tier in ("A", "B", "C", "candidates"):
        for e in getattr(wl, tier):
            if e.domain:
                known_by_domain[e.domain.lower()] = (e.name, db.slugify(e.name))

    stats = {
        "queries": 0, "results": 0, "signals_new": 0,
        "candidates": [], "matched": [],
    }
    for q in queries_for_today(profile, per_day):
        r = soft_get(client, _API, params={"key": key, "cx": cx, "q": q, "num": 10})
        stats["queries"] += 1
        if r is None:
            continue
        try:
            items = r.json().get("items") or []
        except ValueError:
            continue
        for item in items:
            link = item.get("link") or ""
            stats["results"] += 1
            domain = urlparse(link).netloc.lower().removeprefix("www.").rstrip(".")
            if not domain or is_blocked(domain):
                continue
            title = item.get("title") or ""
            if domain in known_domains:
                name, slug = known_by_domain[domain]
                if db.upsert_signal(conn, kind="search", key=link, company_id=slug,
                                    note=f"CSE {q!r}: {title}"):
                    stats["signals_new"] += 1
                stats["matched"].append(f"{name} (search): {title[:70]}")
            elif len(stats["candidates"]) < MAX_CSE_CANDIDATES:
                name = domain.split(".")[0].replace("-", " ").title()
                added, _tokens = add_candidate(
                    name, domain,
                    note=f"CSE {q!r}: {title[:90]}",
                )
                if added:
                    stats["candidates"].append(f"{name} ({domain})")
        time.sleep(1.0)  # Google quota politeness
    return stats
