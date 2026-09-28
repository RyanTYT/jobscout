"""Google News RSS monitoring for watchlist companies — free, no auth.

For each watchlist company, search Google News RSS for "{company_name}
hiring" — catches news articles about expansion, fundraising, headcount
growth. These are the earliest signals ("Company X raises $50M, plans to
double engineering team").
"""

from __future__ import annotations

import re
import sqlite3
import time
import xml.etree.ElementTree as ET

import httpx

from jobscout.core import db
from jobscout.sources.ats.base import soft_get

_RSS_URL = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

# Keywords that make a headline relevant to hiring (vs. general company news)
_HIRING_RE = re.compile(
    r"\b(hiring|hire|hires|hired|recruit|recruiting|expand|expansion|"
    r"growth|growing|headcount|layoff|layoffs|funding|raises|raised|"
    r"round|series|acquisition|acquires|ipo|open|opening|launch|"
    r"office|team|engineering|developer|jobs|role|position|careers)\b",
    re.I,
)


def _parse_rss(xml_text: str) -> list[dict]:
    """Google News RSS → [{title, link, published}]"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    entries: list[dict] = []
    for el in root.iter():
        if el.tag.split("}")[-1] != "item":
            continue
        fields: dict[str, str] = {}
        for ch in el:
            ct = ch.tag.split("}")[-1]
            val = (ch.text or "").strip()
            if ct == "title" and val:
                fields["title"] = val
            elif ct == "link" and val:
                fields["link"] = val
            elif ct == "pubDate" and val:
                fields["published"] = val
        if fields.get("title") and fields.get("link"):
            entries.append(fields)
    return entries[:15]


def sweep(client: httpx.Client, conn: sqlite3.Connection, wl) -> dict:
    """For each watchlist company, search Google News for hiring mentions.

    Emits `news` signals for articles that match both the company name
    and a hiring-relevant keyword. Deduplicated by article URL (natural).
    """
    stats = {"companies_checked": 0, "articles_found": 0, "signals_new": 0,
             "matched": []}

    entries = [
        (e.name, e.domain)
        for tier in ("A", "B", "C", "candidates")
        for e in getattr(wl, tier) if e.domain
    ]

    for name, _domain in entries:
        query = f'"{name}" hiring'
        url = _RSS_URL.format(query=query.replace(" ", "+"))
        r = soft_get(client, url)
        if r is None:
            continue

        stats["companies_checked"] += 1
        articles = _parse_rss(r.text)

        for article in articles:
            title = article["title"]
            # check the title mentions the company AND a hiring keyword
            if name.lower() not in title.lower():
                continue
            if not _HIRING_RE.search(title):
                continue

            stats["articles_found"] += 1
            link = article["link"]
            published = article.get("published", "")
            is_new = db.upsert_signal(
                conn, kind="news", key=link,
                company_id=db.slugify(name),
                note=f"{title}" + (f" ({published})" if published else ""),
            )
            if is_new:
                stats["signals_new"] += 1
                stats["matched"].append(f"{name} (news): {title[:80]}")

        time.sleep(0.4)  # politeness

    return stats
