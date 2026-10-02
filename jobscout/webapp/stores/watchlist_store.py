"""webapp/watchlist_store.py — form adapter for the company editor.

The entry-mutation logic (find/render/tier-move/rollback) lives in ONE
place: jobscout.core.watchlist.update_entry. This module only translates
form fields (comma text, per-line ats pairs) into that call.
"""

from __future__ import annotations

from jobscout.core import watchlist as wlmod

TIERS = wlmod.TIER_KEYS


class WatchlistStoreError(Exception):
    pass


def current():
    return wlmod.load()


def find(slug: str):
    wl = wlmod.load()
    hit = wlmod._find_entry_in(wl, slug)
    if hit[0] is None:
        raise WatchlistStoreError(f"no company with id {slug!r} "
                                  "in watchlist.yaml")
    return hit


def save(slug: str, *, name: str, domain: str, tier: str, note: str,
         ats_text: str) -> dict:
    """Edit/move one entry. ats_text: `provider: token` per line."""
    from jobscout.core.db import slugify

    name = (name or "").strip()
    if not name:
        raise WatchlistStoreError("name required")
    if slugify(name) != slug:
        raise WatchlistStoreError(
            "renaming would change the company id — edit the yaml by hand")
    domain = (domain or "").strip()
    note = (note or "").strip()
    ats: dict[str, str] = {}
    for line in (ats_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" not in line:
            raise WatchlistStoreError(
                f"ATS line must be `provider: token` — got {line!r}")
        provider, _, token = line.partition(":")
        provider, token = provider.strip(), token.strip()
        if not provider or not token:
            raise WatchlistStoreError(
                f"ATS line must be `provider: token` — got {line!r}")
        ats[provider] = token

    try:
        return wlmod.update_entry(slug, tier=tier, domain=domain,
                                  note=note, ats=ats)
    except wlmod.WatchlistError as e:
        raise WatchlistStoreError(str(e)) from e


def promote_many(slug_tiers: dict[str, str]) -> dict:
    """Move several companies into watchlist tiers from a report.

    A company the agent surfaced may be in the DB but absent from
    watchlist.yaml, so this adds it when it is not already listed and moves it
    when it is. watchlist.yaml is written once at the end; the DB tier is
    updated per company so the inbox and the reports agree afterwards.

    Returns {"promoted": [(name, tier)], "skipped": [reason]}.
    """
    from jobscout.core import db
    from jobscout.core.schema import WatchlistEntry

    if not slug_tiers:
        return {"promoted": [], "skipped": []}

    wl = wlmod.load()
    promoted: list[tuple[str, str]] = []
    skipped: list[str] = []
    db.init_db()
    conn = db.connect()
    try:
        for slug, tier in slug_tiers.items():
            if tier not in ("A", "B", "C"):
                skipped.append(f"{slug}: no tier chosen")
                continue
            row = conn.execute(
                "SELECT name, domain, notes FROM companies WHERE id = ?",
                (slug,)).fetchone()
            if row is None:
                skipped.append(f"{slug}: not in the database")
                continue
            name = row["name"]
            if wlmod.find(wl, name) is not None:
                moved = wlmod.promote(wl, name, tier)
                if not moved:
                    skipped.append(f"{name}: already tier {tier}")
                    continue
            else:
                wlmod.add(wl, WatchlistEntry(
                    name=name, domain=row["domain"],
                    note=row["notes"] or "promoted from a run report"), tier)
            db.update_company_tier(conn, slug, tier)
            promoted.append((name, tier))
        if promoted:
            wlmod.save(wl)
    except wlmod.WatchlistError as e:
        raise WatchlistStoreError(str(e)) from e
    finally:
        conn.close()
    return {"promoted": promoted, "skipped": skipped}
