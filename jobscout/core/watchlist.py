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


# ── entry editing (the single mutation layer — UI delegates here) ─────────


def update_entry(slug: str, *, tier: str, domain: str | None, note: str | None,
                 ats: dict[str, str] | None) -> dict:
    """Edit/move one entry by slug. Tier moves relocate the entry block
    between lists (A/B/C/candidates); renaming is rejected — a new name
    would change the company id. Writes with round-trip validation and
    restores the file on any failure."""
    import re


    if tier not in TIER_KEYS:
        raise WatchlistError(f"invalid tier: {tier!r}")

    p = path()
    original = p.read_text(encoding="utf-8")
    lines = original.splitlines()
    found = _find_entry(lines, slug)
    if found is None:
        raise WatchlistError(f"no entry for {slug!r}")
    old_tier, start, end = found
    rendered = _render_entry(slug, domain or "", note or "", ats or {})

    if old_tier == tier:
        lines[start:end] = rendered
    else:
        lines[start:end] = []
        tier_i = _tier_line(lines, tier)
        if tier_i is None:
            raise WatchlistError(f"tier {tier!r} not found")
        if re.search(r"\[\]", lines[tier_i]):          # empty list `B: []`
            lines[tier_i:tier_i + 1] = [f"{tier}:"] + rendered
        else:
            insert = tier_i + 1
            while insert < len(lines):
                nxt = lines[insert]
                if re.match(r"^- name:", nxt):
                    insert += 1
                    while insert < len(lines) and not (
                            re.match(r"^- name:", lines[insert])
                            or re.match(r"^\w", lines[insert])):
                        insert += 1
                else:
                    break
            lines[insert:insert] = rendered

    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        w = load()
        new_tier, entry = _find_entry_in(w, slug)
        if new_tier != tier:
            raise WatchlistError("entry lost after edit — file restored")
        return {"tier": new_tier, "entry": entry}
    except Exception:
        p.write_text(original, encoding="utf-8")
        raise WatchlistError(
            "rejected by validation — file restored") from None


class WatchlistError(Exception):
    pass


def _find_entry_in(w: Watchlist, slug: str):
    for k in TIER_KEYS:
        for e in getattr(w, k):
            if slugify(e.name) == slug:
                return k, e
    return None, None


def _find_entry(lines: list[str], slug: str):
    import re

    tier_at = None
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(rf"^({'|'.join(TIER_KEYS)}):", line)
        if m:
            tier_at = m.group(1)
        em = re.match(r"^- name:\s*(.+)$", line)
        if em and tier_at:
            raw = em.group(1).strip().strip("'\"")
            if slugify(raw) == slug:
                end = i + 1
                while end < len(lines):
                    nxt = lines[end]
                    if re.match(r"^- name:", nxt) or re.match(r"^\w", nxt):
                        break
                    end += 1
                return tier_at, i, end
        i += 1
    return None


def _tier_line(lines: list[str], tier: str) -> int | None:
    import re

    pat = re.compile(rf"^{tier}:")
    for i, line in enumerate(lines):
        if pat.match(line):
            return i
    return None


def _quote(s: str) -> str:
    """Quote a YAML scalar only when the plain form would misparse."""
    import re as _re

    if s and _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .\-+/&;,'()]*", s or ""):
        return s
    return '"' + (s or "").replace('"', '\\"') + '"'


def _render_entry(slug: str, domain: str, note: str,
                  ats: dict[str, str]) -> list[str]:
    """Render an entry block; the name is derived back from the slug's
    owner (the caller rejected renames, so the stored name is preserved
    by never rewriting the - name line — see update_entry's found block:
    the name line is INSIDE [start:end] and gets re-rendered from the
    CURRENT file, so the display name survives)."""
    # the name must be recovered from the file (renames are rejected)
    w = load()
    _, entry = _find_entry_in(w, slug)
    name = entry.name if entry else slug
    out = [f"- name: {_quote(name)}"]
    out.append(f"  domain: {_quote(domain) if domain else 'null'}")
    if ats:
        out.append("  ats:")
        for provider, token in ats.items():
            out.append(f"    {provider}: {token}")
    else:
        out.append("  ats: {}")
    out.append(f"  note: {_quote(note) if note else 'null'}")
    return out
