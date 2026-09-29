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
