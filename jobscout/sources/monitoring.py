"""Monitoring: watch known companies for changes (PLAN §6.1 extension).

Two stateful checkers, both storing per-company state in the existing
`state` table (no schema migration needed):

1. check_page_changes() — for dark-pool companies with a career_url:
   fetch the page → hash → compare with stored hash → if changed,
   extract job-title-like text from both versions and diff → signal

2. check_sitemap_changes() — for dark-pool companies:
   fetch sitemap → extract job-URLs → compare with stored set → new
   URLs → signal (catches drafts before the posting goes live)

State keys in the `state` table:
   page_hash:{company_id}    — sha256 of the careers page text
   page_titles:{company_id}  — JSON list of job-title-like strings
   sitemap_urls:{company_id} — JSON list of known job URLs

Signal kinds:
   careers_page_changed — key: {company_id}:{new_hash[:8]}
   sitemap_new_url      — key: the URL itself
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from hashlib import sha256

import httpx

from jobscout.core import db
from jobscout.sources.ats.base import soft_get, strip_html


def run_monitoring(client: httpx.Client, conn: sqlite3.Connection) -> dict:
    """Orchestrator — called from run.py after the discovery sweep."""
    stats: dict = {}
    try:
        stats["page_changes"] = check_page_changes(client, conn)
    except Exception as e:  # noqa: BLE001
        stats["page_changes"] = {"error": str(e)}
    try:
        stats["sitemap"] = check_sitemap_changes(client, conn)
    except Exception as e:  # noqa: BLE001
        stats["sitemap"] = {"error": str(e)}
    return stats


# ── job-title extraction (deterministic, no LLM) ────────────────────────────

TITLE_RE = re.compile(
    r"(?:senior|staff|principal|lead|junior|mid|entry)?\s*"
    r"(?:software|systems|quant|quantitative|data|platform|infrastructure|"
    r"backend|back[\s-]?end|frontend|full[\s-]?stack|devops|sre|security|"
    r"machine\s+learning|research|trading|execution|network|fpga|embedded)\s*"
    r"(?:engineer|developer|scientist|architect|analyst|manager|trader|"
    r"specialist|programmer|administrator)\b",
    re.I,
)


def extract_job_titles(text: str) -> list[str]:
    """Extract job-title-like strings from page text. Deduplicated, sorted."""
    if not text:
        return []
    seen: set[str] = set()
    for m in TITLE_RE.finditer(text):
        title = m.group(0).strip()
        # normalize whitespace
        title = re.sub(r"\s+", " ", title)
        seen.add(title)
    return sorted(seen)


# ── 1. careers page change detector ──────────────────────────────────────────


def check_page_changes(client: httpx.Client, conn: sqlite3.Connection) -> dict:
    """For dark-pool companies with a career_url: detect page content changes.

    First run: stores the hash and titles (no signal — establishing baseline).
    Subsequent runs: if hash differs, signals with the job-title diff.
    """
    stats = {"companies_checked": 0, "changes_detected": 0, "signals_new": 0}
    rows = conn.execute(
        "SELECT id, name, domain, career_url FROM companies "
        "WHERE non_ats = 1 AND career_url IS NOT NULL AND career_url != ''"
    ).fetchall()

    for row in rows:
        url = row["career_url"]
        r = soft_get(client, url)
        if r is None:
            continue  # page unreachable — honest skip

        stats["companies_checked"] += 1
        text = strip_html(r.text)
        new_hash = sha256(text.encode("utf-8")).hexdigest()
        state_key_hash = f"page_hash:{row['id']}"
        state_key_titles = f"page_titles:{row['id']}"
        old_hash = db.get_state(conn, state_key_hash)

        if old_hash is None:
            # first time — baseline, no signal
            db.set_state(conn, state_key_hash, new_hash)
            db.set_state(conn, state_key_titles,
                         json.dumps(extract_job_titles(text)))
            continue

        if old_hash == new_hash:
            continue  # unchanged

        # page changed — extract and diff job titles
        old_titles = json.loads(db.get_state(conn, state_key_titles) or "[]")
        new_titles = extract_job_titles(text)
        added = [t for t in new_titles if t not in set(old_titles)]
        removed = [t for t in old_titles if t not in set(new_titles)]

        note = "careers page changed"
        if added:
            note += f": +{len(added)} new"
            if len(added) <= 5:
                note += f" ({'; '.join(added)})"
            else:
                note += f" ({'; '.join(added[:3])} +{len(added)-3} more)"
        if removed:
            note += f", -{len(removed)} removed"
        if not added and not removed:
            note += " (content changed, no title diff — restructuring?)"

        signal_key = f"{row['id']}:{new_hash[:8]}"
        is_new = db.upsert_signal(
            conn, kind="careers_page_changed", key=signal_key,
            company_id=row["id"], note=note,
        )
        if is_new:
            stats["signals_new"] += 1
        stats["changes_detected"] += 1

        # update state
        db.set_state(conn, state_key_hash, new_hash)
        db.set_state(conn, state_key_titles, json.dumps(new_titles))

        time.sleep(0.5)  # politeness per company

    return stats


# ── 2. sitemap diff for job URLs ────────────────────────────────────────────


# reuse the sitemap URL extraction from careers_page.py
_SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml")
_JOB_URL_RE = re.compile(r"/job|/career|/vacanc|/position|/opening", re.I)


def _sitemap_job_urls(domain: str, client: httpx.Client) -> list[str]:
    """Fetch sitemap.xml and extract job-related URLs."""
    urls: list[str] = []
    for root in (f"https://{domain}", f"https://www.{domain}"):
        for path in _SITEMAP_PATHS:
            r = soft_get(client, root + path)
            if r is None:
                continue
            locs = _parse_locs(r.text)
            if "<sitemapindex" in r.text[:1000].lower():
                # sitemap index → fetch a few children
                for child in locs[:5]:
                    rc = soft_get(client, child)
                    if rc is not None:
                        urls.extend(_parse_locs(rc.text))
                    time.sleep(0.2)
            urls.extend(locs)
            if urls:
                break
        if urls:
            break
    return [u for u in urls if u.startswith("http") and _JOB_URL_RE.search(u)][:50]


def _parse_locs(xml_text: str) -> list[str]:
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for el in root.iter():
        if el.tag.split("}")[-1] == "loc" and el.text:
            out.append(el.text.strip())
    return out


def check_sitemap_changes(client: httpx.Client, conn: sqlite3.Connection) -> dict:
    """For dark-pool companies: diff their sitemap for new job URLs.

    New URLs appearing in the sitemap catch drafts before the posting goes
    live — CMS generates the URL when the draft is saved.
    """
    stats = {"companies_checked": 0, "new_urls": 0, "signals_new": 0}
    rows = conn.execute(
        "SELECT id, name, domain FROM companies "
        "WHERE non_ats = 1 AND domain IS NOT NULL AND domain != ''"
    ).fetchall()

    for row in rows:
        urls = _sitemap_job_urls(row["domain"], client)
        if not urls:
            continue
        stats["companies_checked"] += 1

        state_key = f"sitemap_urls:{row['id']}"
        old_urls = set(json.loads(db.get_state(conn, state_key) or "[]"))
        new = [u for u in urls if u not in old_urls]

        for u in new:
            is_new = db.upsert_signal(
                conn, kind="sitemap_new_url", key=u,
                company_id=row["id"],
                note=f"new job URL in sitemap: {u}",
            )
            if is_new:
                stats["signals_new"] += 1
        stats["new_urls"] += len(new)

        # update stored URLs (replace, not extend — old removed URLs drop)
        db.set_state(conn, state_key, json.dumps(urls))
        time.sleep(0.3)

    return stats
