"""config/watchlist.yaml read/write — the living company-first substrate (PLAN §5.9).

The file serves BOTH discovery loops:
  * entries WITH `ats:` tokens → daily scrape targets (free ATS JSON APIs)
  * entries WITHOUT tokens → dark-pool graph (careers crawl P3, signals P4/P5)

Discovery appends `candidates` daily via `jobscout add-company`; the owner
promotes to A/B/C. The file is regenerated with a stable header; git history
of this file is the auditable growth of the target-company network.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from jobscout.core import paths as _paths
from jobscout.core.db import slugify
from jobscout.core.schema import Watchlist, WatchlistEntry

TIER_KEYS = ("A", "B", "C", "candidates")

HEADER = """\
# jobscout watchlist — the living company-first substrate (PLAN §5.9)
#
# BOTH jobs live in this one file:
#   1. SCRAPE LIST — entries with `ats:` tokens are polled daily via free ATS JSON APIs.
#   2. DARK-POOL GRAPH — entries with no `ats:` tokens have no board presence:
#      monitored via careers pages (P3) and signals (P4/P5). Hiring signals + no
#      postings = dark-pool lead.
#
# Discovery (pipeline + morning agent) appends entries to `candidates` every day
# via `jobscout add-company <name> --domain <d>` (ATS boards probed automatically).
# Promote with `jobscout add-company <name> --tier B`, or edit this file by hand.
# Per-entry `note:` is data (survives regeneration); free-form comments do not.
#
# Entry fields: name, domain, ats (provider -> board token), note, found_via.
# Git history of this file = the auditable record of your network growing.

"""


def path() -> Path:
    return _paths.config_dir() / "watchlist.yaml"


def load() -> Watchlist:
    p = path()
    if not p.is_file():
        return Watchlist()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return Watchlist.model_validate(data)


def save(w: Watchlist) -> Path:
    p = path()
    payload = {
        k: [e.model_dump(exclude_none=True) for e in getattr(w, k)] for k in TIER_KEYS
    }
    body = yaml.safe_dump(
        payload, sort_keys=False, default_flow_style=False, allow_unicode=True
    )
    p.write_text(HEADER + body, encoding="utf-8")
    return p


def find(w: Watchlist, name: str) -> tuple[str, WatchlistEntry] | None:
    target = slugify(name)
    for k in TIER_KEYS:
        for e in getattr(w, k):
            if slugify(e.name) == target:
                return k, e
    return None


def add(w: Watchlist, entry: WatchlistEntry, tier: str) -> bool:
    """Returns False if a company with the same slug is already listed."""
    if find(w, entry.name) is not None:
        return False
    if tier in ("A", "B", "C"):
        getattr(w, tier).append(entry)
    else:
        w.candidates.append(entry)
    return True


def promote(w: Watchlist, name: str, tier: str) -> bool:
    found = find(w, name)
    if not found:
        return False
    k, e = found
    if k == tier:
        return False
    getattr(w, k).remove(e)
    getattr(w, tier).append(e)
    return True
