"""webapp/watchlist_store.py — company editor (config/watchlist.yaml).

The watchlist is the living company-first substrate; this store edits
one entry at a time: identity (name/domain), tier placement (A/B/C or
candidates), the note (data that survives regeneration), and ATS board
tokens. Tier moves physically relocate the entry block between lists.
Line-level edits; load_watchlist round-trip validation with rollback.
"""

from __future__ import annotations

import re
from pathlib import Path

from jobscout.core import config as core_config
from jobscout.core import paths as core_paths
from jobscout.core.db import slugify

TIERS = ("A", "B", "C", "candidates")


class WatchlistStoreError(Exception):
    pass


def _path() -> Path:
    p = Path(core_paths.config_dir()) / "watchlist.yaml"
    if not p.is_file():
        raise WatchlistStoreError(f"missing config file: {p}")
    return p


def current():
    return core_config.load_watchlist()


def find(slug: str):
    """(tier, WatchlistEntry) for the entry whose slug matches."""
    wl = current()
    for tier in TIERS:
        for e in getattr(wl, tier):
            if slugify(e.name) == slug:
                return tier, e
    raise WatchlistStoreError(f"no company with id {slug!r} in watchlist.yaml")


def _yaml_inline(s: str) -> str:
    if s and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .\-+/&;,'()]*", s):
        return s
    return '"' + (s or "").replace('"', '\\"') + '"'


def _render_entry(name: str, domain: str, note: str,
                  ats: dict[str, str]) -> list[str]:
    lines = [f"- name: {_yaml_inline(name)}"]
    lines.append(f"  domain: {_yaml_inline(domain) if domain else 'null'}")
    if ats:
        lines.append("  ats:")
        for provider, token in ats.items():
            lines.append(f"    {provider}: {token}")
    else:
        lines.append("  ats: {}")
    lines.append(f"  note: {_yaml_inline(note) if note else 'null'}")
    return lines


def _tier_key_lines(lines: list[str]) -> dict[str, int]:
    """Map tier key -> line index (column-0 `A:` style)."""
    out: dict[str, int] = {}
    for i, line in enumerate(lines):
        m = re.match(rf"^({'|'.join(TIERS)}):\s*(\[\])?\s*$", line)
        if m:
            out.setdefault(m.group(1), i)
    return out


def _find_entry(lines: list[str], slug: str):
    """(tier, start, end) line range of the entry block for slug."""
    tier_at = None
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(rf"^({'|'.join(TIERS)}):", line)
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


def save(slug: str, *, name: str, domain: str, tier: str, note: str,
         ats_text: str) -> dict:
    """Edit/move one entry. ats_text: `provider: token` per line."""
    if tier not in TIERS:
        raise WatchlistStoreError(f"invalid tier: {tier!r}")
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

    path = _path()
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()

    found = _find_entry(lines, slug)
    if found is None:
        raise WatchlistStoreError(f"no entry for {slug!r} — file drifted?")
    old_tier, start, end = found
    rendered = _render_entry(name, domain, note, ats)

    if old_tier == tier:
        lines[start:end] = rendered
    else:
        lines[start:end] = []           # remove from the old tier list
        tier_keys = _tier_key_lines(lines)
        if tier not in tier_keys:
            raise WatchlistStoreError(f"tier {tier!r} not found in file")
        ti = tier_keys[tier]
        if re.search(r"\[\]", lines[ti]):        # empty list `B: []`
            lines[ti:ti + 1] = [f"{tier}:"] + rendered
        else:
            insert = ti + 1
            while insert < len(lines):
                nxt = lines[insert]
                if re.match(r"^- name:", nxt):
                    insert += 1
                    # continue past the block
                    while insert < len(lines) and not (
                            re.match(r"^- name:", lines[insert])
                            or re.match(r"^\w", lines[insert])):
                        insert += 1
                else:
                    break
            lines[insert:insert] = rendered

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        new_tier, entry = find(slug)
        if (new_tier != tier or entry.domain != domain
                or (entry.note or "") != note
                or (entry.ats or {}) != ats):
            raise core_config.ConfigError("round-trip mismatch")
        return {"tier": new_tier, "entry": entry}
    except WatchlistStoreError:                   # find failed = broken move
        path.write_text(original, encoding="utf-8")
        raise WatchlistStoreError(
            "entry lost after edit — file restored") from None
    except Exception as e:                        # rollback
        path.write_text(original, encoding="utf-8")
        raise WatchlistStoreError(
            f"rejected by validation — file restored: {e}") from e
