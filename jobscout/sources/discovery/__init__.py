"""Deterministic discovery (PLAN §6.1, §10 P4): RSS + HN + CSE → signals +
watchlist candidates. The agent layer (P5) rides ON TOP of this.

Contract (PLAN §5.9): new companies discovered deterministically enter
watchlist `candidates` (live-probed when a domain is known); promotion
to A/B/C stays the owner's call.
"""

from __future__ import annotations

import httpx

from jobscout import watchlist as wlmod
from jobscout.core import db
from jobscout.core.models import ProfileCfg, Settings, WatchlistEntry
from jobscout.sources.ats.base import soft_get  # noqa: F401 — used by rss/hn/cse
from jobscout.sources.discovery import cse, hn, rss
from jobscout.sources.discovery.blocklist import BLOCKED_DOMAINS, is_blocked  # noqa: F401

MAX_HN_CANDIDATES = 6
MAX_CSE_CANDIDATES = 5


def add_candidate(
    conn,
    wl,
    name: str,
    domain: str | None,
    found_via: str,
    note: str = "",
    client: httpx.Client | None = None,
) -> tuple[bool, dict]:
    """Add a discovered company to watchlist `candidates` (live-probed).

    Returns (added, ats_tokens). Existing companies are left untouched.
    """
    from jobscout.ats_probe import probe_company

    if wlmod.find(wl, name) is not None:
        return False, {}
    if domain:
        # same domain under a different name → same company, don't duplicate
        for tier_key in wlmod.TIER_KEYS:
            for e in getattr(wl, tier_key):
                if e.domain and e.domain.lower() == domain.lower():
                    return False, {}
    tokens: dict[str, str] = {}
    if domain and client is not None:
        try:
            tokens = probe_company(name, domain, client=client)["tokens"]
        except Exception:  # noqa: BLE001 — probing is best-effort
            tokens = {}
    entry = WatchlistEntry(
        name=name,
        domain=domain,
        ats=tokens,
        note=note or f"discovered via {found_via}",
        found_via=found_via,
    )
    wlmod.add(wl, entry, "candidate")
    db.upsert_company(
        conn, name=name, domain=domain, tier="candidate",
        ats_tokens=tokens or None, notes=entry.note,
    )
    return True, tokens


def run_sweep(
    client: httpx.Client,
    conn,
    wl,
    profile: ProfileCfg,
    settings: Settings,
    env: dict | None = None,
) -> dict:
    """Run RSS + HN + CSE sweeps. Caller saves the watchlist when
    watchlist_changed is True. Each source is isolated — one failing
    source never breaks the others."""
    env = env or {}
    out: dict = {
        "rss": None, "hn": None, "cse": None,
        "watchlist_changed": False, "candidates": [], "matched": [],
    }
    hn_candidate_count = 0

    def _hn_add(name: str, domain: str | None, note: str = "") -> tuple[bool, dict]:
        nonlocal hn_candidate_count
        if hn_candidate_count >= MAX_HN_CANDIDATES:
            return False, {}
        added, tokens = add_candidate(conn, wl, name, domain, "hn-whoishiring", note, client)
        if added:
            hn_candidate_count += 1
        return added, tokens

    def _cse_add(name: str, domain: str | None, note: str = "") -> tuple[bool, dict]:
        return add_candidate(conn, wl, name, domain, "cse", note, client)

    try:
        feeds = settings.discovery.pipeline.rss_feeds or list(rss.DEFAULT_FEEDS)
        out["rss"] = rss.sweep(client, conn, wl, feeds)
        out["matched"].extend(out["rss"].get("matched", [])[:10])
    except Exception as e:  # noqa: BLE001
        out["rss"] = {"error": str(e)}

    try:
        out["hn"] = hn.sweep(client, conn, wl, profile, add_candidate=_hn_add)
        if out["hn"].get("candidates"):
            out["candidates"].extend(out["hn"]["candidates"])
            out["watchlist_changed"] = True
        out["matched"].extend(out["hn"].get("matched", [])[:10])
    except Exception as e:  # noqa: BLE001
        out["hn"] = {"error": str(e)}

    try:
        if settings.discovery.pipeline.cse_queries_per_day > 0:
            out["cse"] = cse.sweep(
                client, conn, wl, profile, env,
                per_day=settings.discovery.pipeline.cse_queries_per_day,
                add_candidate=_cse_add,
            )
            if out["cse"].get("candidates"):
                out["candidates"].extend(out["cse"]["candidates"][:MAX_CSE_CANDIDATES])
                out["watchlist_changed"] = True
            out["matched"].extend(out["cse"].get("matched", [])[:10])
        else:
            out["cse"] = {"skipped": "cse_queries_per_day = 0"}
    except Exception as e:  # noqa: BLE001
        out["cse"] = {"error": str(e)}

    return out
