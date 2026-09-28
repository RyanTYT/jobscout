"""HN "Who is hiring" discovery via Algolia (PLAN §6.1, §10 P4).

Monthly thread, comment format "Company | Location | Remote | Role".
Deterministic value: comments that match the profile keywords yield
(1) signals on known companies, (2) NEW watchlist candidates when the
comment contains a company URL (domain → live ATS probe).
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

from jobscout.core import db
from jobscout.sources.ats.base import soft_get
from jobscout.sources.discovery.blocklist import is_blocked

_SEARCH = "https://hn.algolia.com/api/v1/search"

_URL_RE = re.compile(r"https?://(?:www\.)?([a-zA-Z0-9\-]+\.[a-zA-Z0-9\-.]+)")
_EMAIL_RE = re.compile(r"[\w.+-]+@([a-zA-Z0-9\-]+\.[a-zA-Z0-9\-.]+)")
_BARE_DOMAIN_RE = re.compile(
    r"(?:^|[\s(<\"'])(?:www\.)?([a-zA-Z0-9\-]{3,}\.(?:com|io|ai|dev|net|org|co))\b"
)
# Company names that are actually role text — fall back to the domain root.
_ROLE_NAME_RE = re.compile(
    r"engineer|developer|scientist|manager|designer|analyst|positions|"
    r"scientists|engineers|developers|hiring|various|multiple", re.I
)

_PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com",
    "proton.me", "protonmail.com", "icloud.com", "me.com", "aol.com", "mail.com",
    "fastmail.com", "yandex.com", "gmx.com",
}


def _domains_from(text: str) -> list[str]:
    """Companies in HN hiring comments rarely hyperlink — extract domains from
    http links, emails (jobs@acme.com), and bare mentions (acme.com/careers)."""
    domains: list[str] = []

    def _add(d: str) -> None:
        d = d.lower().rstrip(".")
        if d.startswith("www."):
            d = d[4:]
        if (d and d not in domains and not is_blocked(d)
                and d not in _PERSONAL_EMAIL_DOMAINS):
            domains.append(d)

    for d in _URL_RE.findall(text or ""):
        _add(d)
    for d in _EMAIL_RE.findall(text or ""):
        _add(d)
    stripped = re.sub(r"https?://\S+", " ", text or "")
    for m in _BARE_DOMAIN_RE.findall(stripped):
        _add(m)
    return domains

_BAD_SEGMENTS = {
    "remote", "hiring", "jobs", "we", "are", "who", "https", "http",
    "www", "onsite", "hybrid", "full-time", "fulltime", "part-time",
}


def _known_map(wl) -> dict[str, tuple[str, str]]:
    """lower(name) → (name, slug), min length 4 to avoid acronym false hits."""
    out: dict[str, tuple[str, str]] = {}
    for key in ("A", "B", "C", "candidates"):
        for e in getattr(wl, key):
            if len(e.name) >= 4:
                out[e.name.lower()] = (e.name, db.slugify(e.name))
    return out


def keyword_match(text: str, profile) -> bool:
    kws: list[str] = []
    kws.extend(profile.target.roles)
    kws.extend(profile.target.stack)
    kws.extend(profile.target.domains)
    t = (text or "").lower()
    return any(k.lower() in t for k in kws if k)


def parse_comment(text: str) -> tuple[str | None, list[str]]:
    """Returns (company, domains). Company from the first line's first
    segment (|, :, or – separated); domains from any link in the text,
    minus blocked hosts."""
    lines = (text or "").strip().splitlines()
    first = lines[0].strip() if lines else ""
    company: str | None = None
    if first:
        seg = re.split(r"\||:|–|—|-", first, maxsplit=1)[0].strip()
        seg = seg.strip("*. ")
        low = seg.lower()
        if 2 <= len(seg) <= 40 and "http" not in low and low not in _BAD_SEGMENTS:
            company = seg
    return company, _domains_from(text)


def _latest_story(client: httpx.Client) -> dict | None:
    r = soft_get(client, _SEARCH, params={"tags": "story,author_whoishiring", "hitsPerPage": 1})
    if r is None:
        return None
    try:
        hit = (r.json().get("hits") or [])[0]
    except (ValueError, IndexError, TypeError):
        return None
    if not hit:
        return None
    return {
        "id": str(hit.get("objectID")),
        "title": hit.get("title") or "",
        "created_at": hit.get("created_at") or "",
    }


def sweep(client: httpx.Client, conn, wl, profile, add_candidate) -> dict:
    story = _latest_story(client)
    if story is None:
        return {"skipped": "no who-is-hiring story found"}
    stats = {
        "story": story["title"], "story_id": story["id"],
        "comments_seen": 0, "comments_matched": 0,
        "signals_new": 0, "candidates": [], "matched": [],
    }
    r = soft_get(
        client, _SEARCH,
        params={"tags": f"comment,story_{story['id']}", "hitsPerPage": 1000},
    )
    if r is None:
        return {"skipped": "comment fetch failed", "story": story["title"]}
    try:
        hits = r.json().get("hits") or []
    except ValueError:
        return {"skipped": "bad comment payload", "story": story["title"]}

    known = _known_map(wl)
    for h in hits:
        text = h.get("comment_text") or ""
        stats["comments_seen"] += 1
        if not keyword_match(text, profile):
            continue
        stats["comments_matched"] += 1
        company, domains = parse_comment(text)
        if not company:
            continue
        comment_id = str(h.get("objectID") or "")
        note = " ".join(text.split())[:140]
        company_l = company.lower()
        known_hit = None
        for name_l, (name, slug) in known.items():
            if name_l in company_l or company_l in name_l:
                known_hit = (name, slug)
                break
        if known_hit:
            if db.upsert_signal(conn, kind="hn", key=comment_id,
                                company_id=known_hit[1], note=note):
                stats["signals_new"] += 1
            stats["matched"].append(f"{known_hit[0]} (hn): {note[:80]}")
        elif domains:
            domain = domains[0]
            name = company
            if _ROLE_NAME_RE.search(name):
                # first line was role text, not a company — name from the domain
                name = domain.split(".")[0].replace("-", " ").title()
            added, _tokens = add_candidate(
                name, domain, note=f"HN Who-is-hiring: {note[:100]}"
            )
            if added:
                stats["candidates"].append(f"{name} ({domain})")
            db.upsert_signal(
                conn, kind="hn", key=comment_id,
                company_id=db.slugify(name), note=note,
            )
            stats["signals_new"] += 1
        else:
            if db.upsert_signal(conn, kind="hn", key=comment_id, note=f"{company} — no link: {note}"):
                stats["signals_new"] += 1
    db.set_state(conn, "hn_last_story", story["id"])
    return stats


def _domains_for_url(url: str) -> str:
    return urlparse(url).netloc


# ── HN company-name search (P8 dark-pool extension) ─────────────────────────

_HIRING_CONTEXT_RE = re.compile(
    r"\b(hiring|hire|hires|looking for|join (?:our|the) team|growing|"
    r"expanding|recruiting|open position|job opening|careers|we'?re growing|"
    r"we are hiring|new role|engineering role)\b",
    re.I,
)


def search_company_mentions(client: httpx.Client, conn, wl) -> dict:
    """Search recent HN comments for watchlist company mentions.

    Different from the Who-is-hiring sweep: this searches for the company
    name anywhere in recent comments (last 7 days), not just in the hiring
    thread. Catches "I work at X and we're growing" in unrelated threads.
    Only signals when a hiring-context keyword is also present.
    """
    import time as _time
    from datetime import UTC, datetime, timedelta

    stats = {"companies_searched": 0, "comments_found": 0,
             "hiring_context": 0, "signals_new": 0, "matched": []}
    week_ago = int((datetime.now(UTC) - timedelta(days=7)).timestamp())

    names = [e.name for tier in ("A", "B", "C", "candidates")
             for e in getattr(wl, tier) if len(e.name) >= 4]
    # dedupe while preserving order
    seen_names: set[str] = set()
    unique_names = [n for n in names if not (n in seen_names or seen_names.add(n))]

    for name in unique_names[:30]:  # cap at 30 to stay within API budget
        r = soft_get(client, _SEARCH, params={
            "query": f'"{name}"',
            "tags": "comment",
            "numericFilters": f"created_at_i>{week_ago}",
            "hitsPerPage": 20,
        })
        if r is None:
            continue
        stats["companies_searched"] += 1
        try:
            hits = r.json().get("hits") or []
        except ValueError:
            continue

        for h in hits:
            text = h.get("comment_text") or ""
            stats["comments_found"] += 1
            if name.lower() not in text.lower():
                continue
            if not _HIRING_CONTEXT_RE.search(text):
                continue
            stats["hiring_context"] += 1
            comment_id = str(h.get("objectID") or "")
            note = " ".join(text.split())[:140]
            is_new = db.upsert_signal(
                conn, kind="hn_mention", key=comment_id,
                company_id=db.slugify(name),
                note=note,
            )
            if is_new:
                stats["signals_new"] += 1
                stats["matched"].append(f"{name} (hn-mention): {note[:80]}")

        _time.sleep(0.3)  # politeness

    return stats
