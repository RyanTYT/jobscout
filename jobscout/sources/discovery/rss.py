"""RSS discovery — funding + niche feeds → signals (PLAN §6.1, §10 P4).

Headlines mentioning a watchlist company become signals on that company
(dark-pool light-up). Headlines about unknown companies become
company_id-NULL signals — unresolved mentions for the owner/agent to
resolve (the deterministic layer can't get a domain from a headline).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import httpx

from jobscout.core import db
from jobscout.sources.postings.base import soft_get

DEFAULT_FEEDS = (
    "https://techcrunch.com/tag/funding/feed/",
    "https://techcrunch.com/tag/fintech/feed/",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
)

_MAX_ENTRIES_PER_FEED = 40

_FUNDING_RE = re.compile(
    r"^(\S[\w&.,'\- ]{1,60}?)\s+(?:raises|raised|closes|secures|lands|nets|banks)\b"
)
_STRIP_PREFIXES = ("fintech startup", "crypto startup", "startup", "cybersecurity startup")


def _entries(xml_text: str) -> list[dict]:
    """RSS <item> and Atom <entry> → [{title, link, published, summary}]."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out: list[dict] = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        fields: dict[str, str] = {}
        for ch in el:
            ct = ch.tag.split("}")[-1]
            val = (ch.text or "").strip() or (ch.get("href") or "").strip()
            if not val:
                continue
            if ct == "title":
                fields["title"] = val
            elif ct == "link":
                fields.setdefault("link", val)
            elif ct in ("pubDate", "published", "updated"):
                fields.setdefault("published", val)
            elif ct in ("summary", "content"):
                fields.setdefault("summary", val)
        if fields.get("title"):
            out.append(fields)
    return out[:_MAX_ENTRIES_PER_FEED]


def _funding_name(title: str) -> str | None:
    m = _FUNDING_RE.match(title.strip())
    if not m:
        return None
    name = m.group(1).strip().rstrip(",;")
    for prefix in _STRIP_PREFIXES:
        if name.lower().startswith(prefix):
            name = name[len(prefix):]
            break
    name = name.strip(" -–—,:")
    if len(name) < 2 or len(name.split()) > 6:
        return None
    return name or None


def _known_companies(wl) -> list[tuple[str, str]]:
    return [
        (e.name, db.slugify(e.name))
        for key in ("A", "B", "C", "candidates")
        for e in getattr(wl, key)
    ]


def sweep(client: httpx.Client, conn, wl, feeds: list[str] | None = None) -> dict:
    feeds = list(feeds or DEFAULT_FEEDS)
    stats = {
        "feeds": 0, "entries": 0, "signals_new": 0,
        "matched": [], "unknown": [],
    }
    known = _known_companies(wl)
    for url in feeds:
        r = soft_get(client, url)
        if r is None:
            continue
        stats["feeds"] += 1
        for entry in _entries(r.text):
            stats["entries"] += 1
            title = entry["title"]
            link = entry.get("link") or url
            hit_name, hit_slug = None, None
            for name, slug in known:
                if re.search(rf"\b{re.escape(name)}\b", title, re.I):
                    hit_name, hit_slug = name, slug
                    break
            if hit_name:
                if db.upsert_signal(conn, kind="funding", key=link,
                                    company_id=hit_slug, note=title):
                    stats["signals_new"] += 1
                stats["matched"].append(f"{hit_name}: {title}")
            else:
                cand = _funding_name(title)
                if cand:
                    if db.upsert_signal(conn, kind="funding", key=link, note=f"{cand} — {title}"):
                        stats["signals_new"] += 1
                    if cand not in stats["unknown"]:
                        stats["unknown"].append(cand)
    return stats
